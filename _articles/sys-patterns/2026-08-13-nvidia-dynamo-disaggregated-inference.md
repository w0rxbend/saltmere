---
title: "NVIDIA Dynamo: the orchestration layer for disaggregated LLM inference"
date: 2026-08-13
track: sys-patterns
summary: "Dynamo is the datacenter-scale serving framework that sits above vLLM, TensorRT-LLM, and SGLang: it disaggregates prefill from decode, routes on KV-cache locality, tiers the KV cache across GPU/CPU/SSD/remote, and autoscales each pool to an SLO."
reading_time: 6
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

Disaggregating prefill and decode onto separate GPU pools is now standard practice, and PagedAttention paging the KV cache is table stakes. But those are *mechanisms*. Running them across dozens of nodes — deciding which decode worker gets a request, where the KV blocks live, and how many prefill workers you need at 3pm — is an *orchestration* problem. NVIDIA Dynamo is the open-source framework built for exactly that layer. It is engine-agnostic: it drives vLLM, TensorRT-LLM, and SGLang workers underneath rather than replacing them. As of this writing the latest stable release is **v1.2.0 (June 2026)**.

## What Dynamo actually owns

Think of Dynamo as the control plane; the inference engine is the data plane. It contributes four pieces the engine alone doesn't give you:

- **Disaggregated serving (E/P/D):** separate encode, prefill, and decode worker pools that scale independently, so you can put many decode GPUs behind a few prefill GPUs (or vice-versa) to match your traffic's input/output ratio.
- **KV-cache-aware smart router:** instead of round-robin, the router scores each worker on queue depth *and* how much of the request's prefix already lives in that worker's KV cache, then makes a probabilistic routing decision to maximize cache reuse.
- **KV Block Manager (KVBM):** a tiered cache manager spanning GPU HBM, host CPU memory, local SSD, and remote/object storage (it now speaks S3- and Azure-style blob APIs), with offloading between tiers.
- **Planner:** an SLO-driven autoscaler that watches load metrics and adjusts the number of prefill/decode workers to hold your latency target without over-provisioning.

## Why cache-aware routing matters

For multi-turn chat and agentic loops, the same long system prompt and history prefix recurs constantly. A blind load balancer scatters those requests, so each worker recomputes prefill for a prefix it could have reused. Dynamo's router treats KV cache locality as a first-class routing signal: send the request to the worker that already holds the most of its prefix. That turns prefix reuse from a per-worker accident into a fleet-wide property — the payoff scales with how much prompt your workload shares.

```bash
# KV-aware routing in front of a pool of workers
python -m dynamo.frontend --router-mode kv --http-port 8000

# A decode worker registers itself with the router (vLLM backend)
python -m dynamo.vllm --model Qwen/Qwen3-32B --is-decode-worker
# ...and a separate prefill pool:
python -m dynamo.vllm --model Qwen/Qwen3-32B --is-prefill-worker
```

The frontend, router, and workers discover each other over Dynamo's distributed runtime; on Kubernetes the same topology is declared as a `DynamoGraphDeployment` and the Planner drives the replica counts.

## Where it sits in the stack

| Layer | Responsibility | Examples |
| --- | --- | --- |
| Orchestration | Routing, disaggregation, KV tiering, autoscale | **Dynamo** |
| Inference engine | Batching, attention kernels, paged KV per node | vLLM, TensorRT-LLM, SGLang |
| Runtime/hardware | Kernels, collectives, GPUs | CUDA, NCCL, NIXL transfer |

The mental model: your engine of choice decides *how one worker runs a batch fast*; Dynamo decides *which worker, holding which cache, in which pool* should run each request — and how big each pool should be. If you have one node, you don't need Dynamo. The moment prefill and decode want different scaling, or your prefixes are worth reusing across machines, that coordination has to live somewhere, and Dynamo is the open, engine-neutral place to put it.

## Adoption caveats

The pieces are decoupled on purpose — KVBM pip-installs standalone — so you can adopt the router without disaggregation, or KV offloading without the Planner. Start there. Full E/P/D disaggregation adds a KV-transfer hop (NIXL) between pools that only pays off at scale and with fast interconnect; measure goodput, not raw latency, before committing.

**Try next:** Stand up two vLLM workers behind `dynamo.frontend --router-mode kv`, replay a multi-turn chat trace, and compare prefill FLOPs and TTFT against round-robin routing.
