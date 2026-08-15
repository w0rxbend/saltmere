---
title: "KV-Cache Offloading with LMCache: Tiering Attention State to CPU and Disk"
date: 2026-08-14
track: sys-patterns
summary: "GPU memory is the bottleneck for LLM serving, and the KV cache is what consumes it. LMCache moves attention state down a tiered hierarchy — GPU, CPU RAM, NVMe, remote — and reuses it across requests and nodes, including non-prefix reuse, through vLLM's V1 KV-connector interface."
reading_time: 6
tags: [llm-serving, lmcache, vllm, kv-cache, offloading, disaggregation, ai-infrastructure]
sources:
  - title: "LMCache — Supercharge Your LLM with the Fastest KV Cache Layer (GitHub)"
    url: "https://github.com/LMCache/LMCache"
  - title: "Offload KV cache to CPU — LMCache quickstart"
    url: "https://docs.lmcache.ai/getting_started/quickstart/offload_kv_cache.html"
  - title: "LMCache examples — vLLM documentation"
    url: "https://docs.vllm.ai/en/latest/examples/disaggregated/lmcache/"
  - title: "Disaggregated Prefilling — vLLM documentation"
    url: "https://docs.vllm.ai/en/stable/features/disagg_prefill/"
  - title: "CacheBlend: Fast LLM Serving for RAG with Cached Knowledge Fusion (arXiv:2405.16444, EuroSys '25)"
    url: "https://arxiv.org/abs/2405.16444"
---

**Gist.** Every token a transformer has processed leaves behind a key/value tensor — the KV cache — that attention consults for every subsequent token, and in production serving that cache is the largest consumer of GPU high-bandwidth memory (HBM), and under long contexts it can exceed the model weights. **[LMCache](https://github.com/LMCache/LMCache)** converts eviction from GPU memory into *demotion* down a tiered hierarchy — CPU DRAM, local NVMe, remote stores — and reloads the blocks when a later request needs them, on the same node or another. The cost is that every tier below HBM is reached across a slower link, so a load that does not amortise its transfer is strictly worse than recomputing the prefill it replaced.

## What the cache is and why it dominates

Decoding is inexpensive per token precisely because prior keys and values are retained rather than recomputed. The cache grows with **sequence length, layer count, head count and batch size**, so the workloads that make an inference service commercially interesting — long documents, agentic loops that append tool output turn after turn, multi-turn chat — are exactly the workloads whose caches grow fastest. When resident KV exceeds what HBM can hold, vLLM evicts blocks. The next request whose prompt covers the evicted span recomputes the prefill in full: **eviction is destructive, and its cost is paid at prefill, the phase that determines time-to-first-token (TTFT)**.

vLLM's built-in prefix caching mitigates the repeat cost but is bounded by HBM capacity, and it matches only a *leading* run of identical tokens. Both limits are structural rather than incidental, and both are what a tiering layer addresses.

## The three problems LMCache addresses

- **Capacity.** GPU HBM is the scarcest and costliest tier per byte. Host DRAM is cheaper and provisioned in larger quantities; local NVMe is larger still. Tiering keeps far more warm KV resident somewhere in the machine than HBM alone permits, at the price of a slower path to it.
- **Cross-request reuse beyond the prefix.** LMCache can reuse cached KV blocks at *any* position in the prompt, following the technique from the [CacheBlend](https://arxiv.org/abs/2405.16444) paper (EuroSys '25): **recomputing the KV of a selected subset of tokens** to restore the cross-attention the cached blocks never saw, so that concatenated retrieval-augmented generation (RAG) chunks reuse their cached KV even when they do not form a shared prefix. Ordinary prefix caching discards all of it the moment chunk order differs.
- **Cross-node sharing.** In disaggregated prefill/decode deployments the prefill instance computes KV that the decode instance requires. LMCache transfers those blocks between instances so the decode pool does not recompute them.

The reported effect is lower TTFT and higher throughput, concentrated on long-context, multi-turn and knowledge-augmented workloads — that is, where **the same context recurs**. No comparable gain is claimed for streams of short, unique prompts, and none should be assumed.

## The connector boundary

LMCache attaches to vLLM through the **V1 KV-connector interface**, registered as `LMCacheConnectorV1`. The connector is the whole integration surface: the engine keeps ownership of scheduling and of the GPU block table, and calls out to store and load KV. Two consequences follow directly. First, adoption is incremental — the engine is not patched, and removing the transfer config restores stock behaviour. Second, **the connector's `kv_role` decides the instance's part in the exchange**: `kv_both` means the instance both stores KV it produces and loads KV others produced, which is the configuration a single-node offload deployment wants.

Cache granularity is a **chunk**, measured in tokens and set by `chunk_size`. The chunk is the unit of storage, lookup and transfer, so it fixes the alignment at which reuse can happen: a matching span shorter than one chunk yields nothing, and an oversized chunk moves bytes the request will not attend to.

### Pure CPU offload

The simplest deployment adds no hardware and spills the KV cache into host RAM. LMCache reads environment variables; vLLM receives a `KVTransferConfig`.

```python
import os
from vllm import LLM
from vllm.config import KVTransferConfig

os.environ["LMCACHE_CHUNK_SIZE"] = "256"          # tokens per KV chunk
os.environ["LMCACHE_LOCAL_CPU"] = "True"          # enable the CPU-RAM tier
os.environ["LMCACHE_MAX_LOCAL_CPU_SIZE"] = "5.0"  # GB of host RAM for KV

ktc = KVTransferConfig(
    kv_connector="LMCacheConnectorV1",
    kv_role="kv_both",   # this instance both stores and loads KV
)

llm = LLM(
    model="Qwen/Qwen3-8B",
    kv_transfer_config=ktc,
    max_model_len=8000,
    gpu_memory_utilization=0.8,
)
```

`gpu_memory_utilization` and `max_local_cpu_size` are the two capacity dials, and they govern different tiers: the first bounds what vLLM claims of HBM for weights plus KV blocks, the second bounds what LMCache claims of host RAM for demoted chunks. **Neither implies the other**, and raising GPU utilisation to reclaim HBM headroom does not enlarge the offload tier.

For an online server the same settings move into a configuration file, with the connector supplied on the command line:

```bash
# lmcache_config.yaml
#   chunk_size: 256
#   local_cpu: true
#   max_local_cpu_size: 5
LMCACHE_CONFIG_FILE=lmcache_config.yaml \
vllm serve Qwen/Qwen3-8B \
  --kv-transfer-config \
  '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}'
```

A request whose context has been seen before now loads its KV from host RAM rather than recomputing it on the GPU. Adding a disk tier means pointing LMCache at a local storage backend; sharing across nodes means backing it with a remote store. **The connector interface is unchanged in both cases** — the tier is a configuration decision, not a different integration.

## The break-even

Offloading is not free, and the hierarchy is ordered by cost: HBM, then the PCIe hop to CPU RAM, then NVMe, then the network. The condition is a single inequality — **loading the KV must be cheaper than recomputing it**. A long shared prefix clears it comfortably, because prefill cost grows with the reused span while transfer cost grows with the bytes moved. A short unique prompt fails it, because there is no reuse to amortise and the lookup itself is the only work done.

That inequality is workload-dependent, so it must be measured rather than assumed. Tune `chunk_size` and the per-tier size caps against the deployment's real context-length distribution, and compare TTFT with the connector enabled against the same prompt set with it disabled, on a **cache-warm** second pass — a cold first pass measures only the store path.

## Pitfalls

- **A short unique prompt is slower with offload than without.** The request pays lookup and transfer, then prefills anyway; there is no reused span to amortise the cost against.
- **Benchmarking a cold cache reports a regression.** The first pass over a prompt set only populates the tiers, so its TTFT includes store overhead and none of the load benefit; the comparison is only meaningful on the warm pass.
- **An oversized `chunk_size` moves KV the request never attends to.** Chunks are the transfer unit, so bandwidth is spent on the whole chunk regardless of how much of it the prompt matches.
- **An undersized `chunk_size` loses reuse at the boundaries.** A matching span shorter than one chunk produces no hit, so real reuse in the workload goes unrecorded.
- **Raising `gpu_memory_utilization` does not enlarge the offload tier.** It bounds HBM only; `max_local_cpu_size` is the separate cap on host RAM, and a small value silently limits how much demoted KV survives.
- **Prefix caching alone discards RAG context when chunk order varies.** It matches only a leading identical run, which is the case CacheBlend's selective KV recomputation addresses and plain prefix reuse cannot.
- **`kv_role` is a role, not a switch.** An instance configured to store but not load will populate the tiers and never read them back, producing overhead with no TTFT gain.
