---
title: "NVIDIA Dynamo: the orchestration layer for disaggregated LLM inference"
date: 2026-08-13
track: sys-patterns
summary: "Dynamo is the datacenter-scale serving framework that sits above vLLM, TensorRT-LLM, and SGLang: it disaggregates prefill from decode, routes on KV-cache locality, tiers the KV cache across GPU/CPU/SSD/remote, and autoscales each pool to an SLO."
reading_time: 5
tags: [llm-serving, nvidia-dynamo, disaggregation, kv-cache, routing, autoscaling, ai-infrastructure]
sources:
  - title: "How NVIDIA Dynamo 1.0 Powers Multi-Node Inference at Production Scale (NVIDIA Technical Blog)"
    url: "https://developer.nvidia.com/blog/nvidia-dynamo-1-production-ready/"
  - title: "Introducing NVIDIA Dynamo, a Low-Latency Distributed Inference Framework (NVIDIA Technical Blog)"
    url: "https://developer.nvidia.com/blog/introducing-nvidia-dynamo-a-low-latency-distributed-inference-framework-for-scaling-reasoning-ai-models"
  - title: "KV Block Manager — NVIDIA Dynamo Documentation"
    url: "https://docs.nvidia.com/dynamo/latest/architecture/kvbm_intro.html"
  - title: "Router Guide — NVIDIA Dynamo Documentation"
    url: "https://docs.nvidia.com/dynamo/v1.0.0/components/router/router-guide"
  - title: "Releases — ai-dynamo/dynamo (GitHub)"
    url: "https://github.com/ai-dynamo/dynamo/releases"
---

**Gist.** Splitting large language model (LLM) inference into a prefill phase and a decode phase, and paging the key/value (KV) attention cache, are per-node mechanisms; running them across dozens of nodes raises a separate question of which worker serves a request, where its KV blocks reside, and how many workers each phase needs. NVIDIA Dynamo is an open-source, engine-agnostic control plane for that layer: it maintains separate prefill and decode pools, routes on KV-cache locality, tiers KV blocks across memory levels, and autoscales pools against a service-level objective (SLO). The cost is a **KV-transfer hop between pools** plus the operational surface of a distributed router, cache manager, and autoscaler that a single-node deployment does not need.

## Division of responsibility

Dynamo is the control plane; the inference engine — vLLM, TensorRT-LLM, or SGLang — remains the data plane and is driven rather than replaced. Four components sit above the engine:

- **Disaggregated serving (prefill/decode, extended to encode/prefill/decode for multimodal input):** separate worker pools per phase that scale independently, so the ratio of decode GPUs to prefill GPUs can be set to match the input/output token ratio of the traffic.
- **KV-cache-aware router:** rather than round-robin, the router scores each worker on **queue depth and on how much of the request's prefix already resides in that worker's KV cache**, then makes a **probabilistic** routing decision that favours cache reuse.
- **KV Block Manager (KVBM):** a tiered cache manager spanning GPU high-bandwidth memory (HBM), host CPU memory, local solid-state disk (SSD), and remote or object storage, with offloading between tiers.
- **Planner:** an SLO-driven autoscaler that observes load metrics and adjusts the prefill and decode replica counts to hold a latency target without over-provisioning.

## The invariant the router maintains

Prefix reuse is the property being defended. In multi-turn chat and agentic loops the same long system prompt and conversation history recur across requests. A load balancer that ignores cache state scatters those requests, so **each worker recomputes prefill for a prefix another worker already holds**. Recomputation is not a correctness failure — the output is identical — it is a throughput failure: prefill floating-point work grows in proportion to the fraction of shared prefix that was discarded.

Dynamo's router promotes KV-cache locality to a first-class routing signal, so that prefix reuse becomes a fleet-wide property instead of a per-worker accident. The magnitude of the gain therefore **scales with how much prompt the workload shares**; a workload of independent short prompts has nothing for the router to exploit.

Two forces pull against each other in the score. Locality alone would pin every request in a hot conversation onto one worker and let its queue grow without bound; queue depth alone reproduces round-robin. Because the decision is **probabilistic** rather than a strict argmax, requests spread across workers of comparable score instead of all landing on the single current best.

```bash
# KV-aware routing in front of a pool of workers
python -m dynamo.frontend --router-mode kv --http-port 8000

# A worker registers itself with the router (vLLM backend); decode is the default role
python -m dynamo.vllm --model Qwen/Qwen3-32B
# ...and a separate prefill pool:
python -m dynamo.vllm --model Qwen/Qwen3-32B --is-prefill-worker
```

The frontend, router, and workers discover each other over Dynamo's distributed runtime. On Kubernetes the same topology is declared as a `DynamoGraphDeployment`, and the Planner drives the replica counts within it.

## Position in the stack

| Layer | Responsibility | Examples |
| --- | --- | --- |
| Orchestration | Routing, disaggregation, KV tiering, autoscale | **Dynamo** |
| Inference engine | Batching, attention kernels, paged KV per node | vLLM, TensorRT-LLM, SGLang |
| Runtime/hardware | Kernels, collectives, GPUs | CUDA, NCCL, NIXL transfer |

The engine determines **how one worker executes one batch**; Dynamo determines **which worker, holding which cache, in which pool** executes each request, and how large each pool is. A single-node deployment has neither decision to make. The coordination becomes necessary once prefill and decode want different scaling factors, or once prefixes are worth reusing across machines.

### Implementation sketch (Scala)

The load-bearing idea in cache-aware routing is that a prompt is reduced to a sequence of **block hashes over fixed-size token blocks**, and a worker's cached prefix coverage is the **length of the longest common prefix of those hashes** with the blocks that worker reports holding. The score then trades that coverage against queue depth. The weighting below is illustrative, not Dynamo's published formula.

```scala
final case class Worker(id: String, cachedBlocks: Set[Long], queueDepth: Int)

/** Fixed-size token blocks, hashed with the prefix folded in, so a block hash
  * identifies the whole prefix ending at that block — not the block alone. */
def blockHashes(tokens: Vector[Int], blockSize: Int): Vector[Long] =
  tokens.grouped(blockSize).filter(_.size == blockSize)
    .scanLeft(0L)((prev, blk) => blk.foldLeft(prev * 31L)(_ * 31L + _))
    .drop(1).toVector

/** Coverage stops at the first miss: a suffix cannot be reused without its prefix. */
def coverage(hashes: Vector[Long], w: Worker): Int =
  hashes.takeWhile(w.cachedBlocks.contains).size

def pick(hashes: Vector[Long], workers: Vector[Worker], rng: scala.util.Random): Worker =
  val scored = workers.map(w => w -> (coverage(hashes, w).toDouble - w.queueDepth))
  val max    = scored.map(_._2).max
  // Sample uniformly among workers within 1.0 of the best score, so a hot prefix
  // does not pin every request of a conversation onto one worker.
  val best   = scored.filter(_._2 >= max - 1.0).map(_._1)
  best(rng.nextInt(best.size))
```

## Adoption path

The components are decoupled: the router can be adopted without disaggregation, or KV offloading without the Planner. Full E/P/D disaggregation introduces a **KV-transfer hop over NIXL (NVIDIA Inference Transfer Library) between the prefill and decode pools**, which pays off only at scale and with a fast interconnect. The quantity to measure before committing is **goodput** — requests completed within the SLO — rather than raw latency, because disaggregation trades a transfer hop for independent pool scaling.

A minimal comparison: two vLLM workers behind `dynamo.frontend --router-mode kv`, a replayed multi-turn chat trace, and prefill floating-point operations and time-to-first-token (TTFT) measured against round-robin routing.

## Pitfalls

- **Workloads without shared prefixes gain nothing from KV-aware routing.** Independent short prompts produce no prefix overlap, so the router's score degenerates to queue depth and the added component earns only its own overhead.
- **Adopting disaggregation on a slow interconnect can lower goodput.** Every disaggregated request pays a KV-transfer hop between the prefill and decode pools; below some interconnect bandwidth that hop costs more than the independent scaling returns.
- **Comparing raw latency instead of goodput hides the trade-off.** Disaggregation changes where time is spent and how many requests fit in a pool; a latency-only comparison can reject a configuration that serves more requests within the SLO.
- **Sizing the prefill and decode pools by an equal split ignores the traffic's token ratio.** The pools exist to be scaled independently against the input/output ratio; an equal split leaves one pool idle while the other is the bottleneck.
- **A single-node deployment inherits the control plane's operational surface with none of its decisions.** With one worker there is no routing choice and no cross-machine cache to reuse.
