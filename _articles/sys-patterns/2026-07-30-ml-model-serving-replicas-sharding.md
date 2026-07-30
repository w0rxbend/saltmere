---
title: "Serving ML models: the same replication and sharding patterns, new constraints"
date: 2026-07-30
track: sys-patterns
summary: "Burns' serving patterns — replicated stateless services, sharded services — apply directly to model inference, but the constraints flip: models are huge, GPUs are scarce and expensive, and the biggest models no longer fit on one machine. Here's how replicas, sharding, and multi-node inference map onto AI serving, with a KServe example."
reading_time: 6
tags: [ai-infrastructure, model-serving, sharding, replication, kserve, gpu]
sources:
  - title: "Designing Distributed Systems (2nd ed.) — Brendan Burns (serving patterns; AI infrastructure)"
    url: "https://www.oreilly.com/library/view/designing-distributed-systems/9781098156343/"
  - title: "Announcing KServe v0.15: Advancing Generative AI Model Serving — CNCF blog (June 2025)"
    url: "https://www.cncf.io/blog/2025/06/18/announcing-kserve-v0-15-advancing-generative-ai-model-serving/"
  - title: "KServe — Multi-node/Multi-GPU Inference documentation"
    url: "https://kserve.github.io/website/docs/model-serving/generative-inference/multi-node"
  - title: "vLLM: Easy, Fast, and Cheap LLM Serving with PagedAttention"
    url: "https://blog.vllm.ai/2023/06/20/vllm.html"
---

Brendan Burns' *Designing Distributed Systems* teaches serving as a small set of patterns: replicate a stateless service behind a load balancer to scale throughput; shard it when the state is too big for one replica to hold. Model inference is "just" a serving workload, so the same patterns apply — but the constraints are inverted enough that applying them naïvely is expensive. Let's map the patterns onto AI serving and see where the constraints bite.

## Replication: the base case, gated by GPUs

An inference server that loads a model into memory and answers requests is stateless in Burns' sense — each request is independent, so you scale by running N identical replicas behind a load balancer. Nothing new there.

What's new is the *unit* you're replicating. A web replica is a few hundred MB of RAM on a CPU you can rent by the hundred. A model replica pins a multi-gigabyte weight file into **GPU** memory, and GPUs are scarce, expensive, and slow to start (pulling a 30 GB image and loading weights can take minutes). Two consequences:

- **Scale-to-zero matters more than it does for web services.** An idle web pod wastes cents; an idle GPU pod wastes dollars per hour. KServe supports scaling inference services to zero when idle precisely because the marginal cost of a warm replica is so high.
- **Autoscaling on RPS is wrong.** Inference latency is dominated by batch size and sequence length, not request count. You scale on GPU utilization or queue depth, and you *batch* requests within a replica (dynamic/continuous batching) before you add another replica.

## Sharding: when the model doesn't fit on one machine

Burns' sharded-service pattern is for when your state is too large for a single replica — you partition it and route each request to the shard that owns its data. The classic example is a cache too big for one node's RAM.

Frontier LLMs hit the identical wall for a different reason: the weights don't fit in one GPU, or even one *machine*. A 400B-parameter model in half precision is ~800 GB — no single GPU holds that. The response is **model-parallel sharding**:

- **Tensor parallelism** splits each layer's matrices across GPUs *within* a node (fast NVLink between them).
- **Pipeline parallelism** splits the model's layers across *multiple nodes*, so a request flows node→node through the pipeline.

This is Burns' sharding pattern with a twist: in a normal sharded cache, a request touches *one* shard. In model sharding, a *single inference* touches *every* shard in sequence — the model is partitioned but the computation is not independent. That makes the interconnect (NVLink, InfiniBand) part of the critical path, and it's why "multi-node inference" is a distinct, harder mode than "more replicas."

## KServe: the patterns as declarative config

KServe (a CNCF project; v0.15 landed in June 2025 with a focus on generative/LLM serving) exposes exactly this hierarchy. A single-node, replicated deployment is the default; you opt into multi-node sharding when a model is too big. The replicated case is a few lines:

```yaml
apiVersion: serving.kserve.io/v1beta1
kind: InferenceService
metadata:
  name: sentiment
spec:
  predictor:
    minReplicas: 0          # scale to zero when idle — GPUs are expensive
    maxReplicas: 8          # replicate for throughput
    scaleTarget: 70         # target GPU/concurrency, not raw RPS
    model:
      modelFormat: { name: huggingface }
      storageUri: "s3://models/sentiment/"
      resources:
        limits: { nvidia.com/gpu: "1" }
```

For a model that needs sharding across machines, KServe's multi-node mode places one InferenceService across a *worker group* of GPU nodes with a tensor/pipeline-parallel runtime (commonly vLLM under the hood). The pattern you chose — replicate vs. shard — becomes a field, not a rewrite.

## The design errors to avoid

Burns closes the book with common failures, and AI serving has its own greatest hits, all of them just the general patterns misapplied:

- **Sharding a model that fits on one GPU.** Model parallelism adds interconnect latency to every token. If the model fits, replicate — sharding is pure overhead you took on for no reason.
- **Replicating a model that doesn't fit.** The pod won't schedule, or it thrashes to host memory. Check the arithmetic (params × bytes-per-param) before you pick replication.
- **Treating cold start as free.** With scale-to-zero, the first request after idle eats the model-load time. Keep one warm replica for latency-sensitive paths, or accept the cold-start tail explicitly.

The lesson is the reassuring one: you don't need a new mental model for AI infrastructure. Replication scales throughput; sharding handles state that won't fit; the routing and load-balancing layers are the ones you already know. What changed is that the "state" is model weights and the "commodity" is a GPU, so the cost of getting the replicate-vs-shard decision wrong went up by two orders of magnitude.

**Try next:** Take one open model (say a 7B that fits in a single GPU) and deploy it on KServe with `minReplicas: 0, maxReplicas: 4`; load-test until it scales up, then idle it and watch it scale to zero — then try to force multi-node sharding on that same too-small model and measure the latency you *added* by parallelizing something that never needed it.
