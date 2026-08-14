---
title: "KV-Cache Offloading with LMCache: Tiering Attention State to CPU and Disk"
date: 2026-08-14
track: sys-patterns
summary: "GPU memory is the bottleneck for LLM serving, and the KV cache is what eats it. LMCache moves attention state down a tiered hierarchy — GPU, CPU RAM, NVMe, remote — and reuses it across requests and nodes, including non-prefix reuse, through vLLM's V1 KV-connector interface."
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

Every token a transformer has already read leaves behind a key/value tensor — the KV cache — that attention reuses for every subsequent token. It's the reason decoding is fast, and it's also the single largest consumer of GPU memory in production serving. For long contexts, agentic loops, and multi-turn chat, the cache dwarfs the model weights. When it won't fit, vLLM evicts it, and the next request that needs that prefix pays the full prefill cost again from scratch.

vLLM's built-in prefix caching helps, but it's bounded by GPU HBM: once a block is evicted, it's gone. **[LMCache](https://github.com/LMCache/LMCache)** turns that hard eviction into a *demotion*. It's a KV-cache management layer that moves attention state out of GPU memory into a tiered hierarchy — CPU DRAM, local NVMe, and remote backends — and pulls it back when a request needs it, across requests and across nodes.

## The three problems it solves

- **Capacity.** GPU HBM is tiny and expensive. CPU RAM is 10–20x larger and cheap; NVMe is larger still. Tiering lets you keep far more warm KV than HBM alone allows.
- **Cross-request reuse beyond the prefix.** Ordinary prefix caching only reuses a *leading* run of identical tokens. LMCache can reuse cached KV blocks at *any* position in the prompt — the trick from the [CacheBlend](https://arxiv.org/abs/2405.16444) paper (EuroSys '25), which selectively recomputes a small fraction of cross-attention so that concatenated RAG chunks reuse their KV even when they aren't a shared prefix.
- **Cross-node sharing.** In disaggregated prefill/decode setups the prefill node computes KV that the decode node needs. LMCache moves those blocks between instances so the decode pool doesn't recompute them.

The payoff LMCache reports is lower **TTFT** (time-to-first-token) and higher throughput, concentrated exactly on long-context, multi-turn, and knowledge-augmented workloads where the same context recurs.

## Enabling it in vLLM

LMCache plugs into vLLM through the **V1 KV-connector** interface using the `LMCacheConnectorV1` connector. The simplest deployment is pure CPU offload — no extra hardware, just spill the KV cache into host RAM. Configure LMCache through environment variables, then hand vLLM a `KVTransferConfig`:

```python
import os
from vllm import LLM
from vllm.config import KVTransferConfig

os.environ["LMCACHE_CHUNK_SIZE"] = "256"        # tokens per KV chunk
os.environ["LMCACHE_LOCAL_CPU"] = "True"        # enable CPU-RAM tier
os.environ["LMCACHE_MAX_LOCAL_CPU_SIZE"] = "5.0"  # GiB of host RAM for KV

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

For an online server, move the same knobs into a config file and pass the connector on the command line:

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

Now when a request's context has been seen before, its KV is loaded from CPU RAM instead of recomputed on the GPU. To add a disk tier, point LMCache at a local storage backend; to share across nodes, back it with a remote store — the connector interface stays the same.

## What to watch

Offloading isn't free. Every tier down the hierarchy is slower: HBM is the fastest, then the PCIe hop to CPU RAM, then NVMe, then network. The break-even is simple — **loading KV must be cheaper than recomputing it.** For a long shared prefix that's an easy win; for a short unique prompt, the transfer can cost more than the prefill it saves. Tune `chunk_size` and the per-tier size caps against your real context-length distribution, and measure TTFT with the connector on versus off before trusting it. Because LMCache rides vLLM's V1 connector API rather than patching the engine, you can adopt it incrementally and roll back by dropping one flag.

**Try next:** Run `vllm serve` twice on a long-context prompt set — once plain, once with `LMCacheConnectorV1` and CPU offload — and compare TTFT on the second, cache-warm pass to see how much prefill you reclaimed from host RAM.
