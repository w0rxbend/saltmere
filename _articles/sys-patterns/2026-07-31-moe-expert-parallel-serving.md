---
title: "Expert parallelism: serving an MoE model whose experts exceed one GPU"
date: 2026-07-31
track: sys-patterns
summary: "A Mixture-of-Experts language model routes each token to a few of hundreds of experts, so the parameter count is large while the per-token compute stays small. Serving such a model requires scattering experts across GPUs and shuffling tokens to the device holding their experts — two all-to-all collectives per layer. This article covers the expert-parallel placement pattern, the dispatch/combine communication, and why load imbalance rather than arithmetic cost bounds throughput."
reading_time: 6
tags: [moe, expert-parallelism, llm-serving, all-to-all, vllm, deepep]
sources:
  - title: "Fedus, Zoph, Shazeer — Switch Transformers: Scaling to Trillion Parameter Models (arXiv:2101.03961)"
    url: "https://arxiv.org/abs/2101.03961"
  - title: "Lepikhin et al. — GShard: Scaling Giant Models with Conditional Computation (arXiv:2006.16668)"
    url: "https://arxiv.org/abs/2006.16668"
  - title: "Expert Parallelism — SGLang documentation"
    url: "https://docs.sglang.io/advanced_features/expert_parallelism.html"
  - title: "Expert Parallel Deployment — vLLM documentation"
    url: "https://docs.vllm.ai/en/latest/serving/expert_parallel_deployment/"
  - title: "DeepEP: an efficient expert-parallel communication library (DeepSeek)"
    url: "https://github.com/deepseek-ai/DeepEP"
---

**Gist.** A Mixture-of-Experts (MoE) model holds many parallel feed-forward experts per layer but activates only a few per token, so its weights are far larger than any single GPU's memory while its per-token arithmetic stays dense-model sized. Expert parallelism (EP) resolves the mismatch by sharding experts across devices and moving tokens to whichever device owns their routed expert, at a cost of **two all-to-all collectives per MoE layer on the critical path**. Because the router decides destinations at runtime and is not obliged to spread tokens evenly, the binding constraint is load imbalance: the collective acts as a barrier, so the slowest device sets the step time.

## The activation pattern that creates the problem

A dense large language model (LLM) applies every parameter to every token. An MoE layer does not: it holds many parallel feed-forward networks (the experts), and a small **router** — a learned linear projection followed by a softmax and a top-k selection — sends each token to only k of them. Switch Transformers use top-1; GShard uses top-2. A layer with 256 experts therefore stores 256 feed-forward weight sets while executing k of them per token.

Two consequences follow, and they pull in opposite directions. The parameter total is too large to replicate on every GPU. Yet **the destination of a token is unknown until the router executes**, so no static assignment of tokens to devices can be correct.

## The placement pattern: experts across GPUs

Expert parallelism shards the experts of each MoE layer across devices: GPU 0 holds experts 0–31, GPU 1 holds 32–63, and so on. Attention and the non-expert layers are replicated, typically via tensor or data parallelism; only the expert feed-forward networks are partitioned. This differs from tensor parallelism, which splits *every* weight matrix across devices. **EP keeps each expert whole and distributes which experts live where**, so an expert's matmul is local and complete once its tokens arrive.

Arrival is the work. A token resident on GPU 0 may route to an expert on GPU 3, making each MoE layer a two-step shuffle:

1. **Dispatch (all-to-all):** every device sends each of its tokens to the device owning that token's chosen expert or experts.
2. **Combine (all-to-all):** after the experts execute, outputs are shuffled back so each token returns to the device owning its sequence, scaled by the router's gate values.

That is **two all-to-all collectives per MoE layer per forward pass**, sitting directly on the critical path, which is why the communication kernel matters as much as the matmul. DeepSeek's **DeepEP** provides tuned all-to-all dispatch and combine kernels for exactly this pattern, with a high-throughput mode for prefill and a low-latency mode for decode. Both vLLM and SGLang accept it as a backend.

## The binding constraint: load imbalance

Arithmetic cost per layer is fixed and predictable; routing is not. Nothing constrains the router to a uniform distribution, and in practice some experts become hot and receive disproportionately many tokens. Under expert parallelism a hot expert is a hot *device*, and **because the all-to-all is a barrier, every device waits for the slowest**. The excess capacity on the remaining devices is unrecoverable for that step.

Frameworks bound the skew with a **capacity factor**. Each expert receives a fixed buffer of `capacity = capacity_factor × (tokens / num_experts)` slots. The fixed buffer keeps the collective rectangular, which is what allows efficient kernels, and it forces a trade-off in both directions:

- Tokens exceeding a hot expert's capacity are **dropped** — they bypass the expert and propagate through the residual connection, so their representation is not transformed by the layer, degrading quality.
- A capacity factor set high enough to avoid drops pads the buffers, and the padding consumes both compute and interconnect bandwidth.

GShard introduces the capacity bound precisely because token-choice routing does not distribute tokens evenly on its own; no published figure fixes the typical skew, which depends on the model and the request mix.

Two mitigations apply at different times. During training, an **auxiliary load-balancing loss** penalises non-uniform expert usage and pushes the router toward an even distribution. At serving time, an **expert-parallel load balancer (EPLB)** rebalances placement rather than routing: it measures per-expert load over a window and either relocates experts or replicates hot ones onto additional devices as **redundant experts**, so no single device is the bottleneck.

### Implementation sketch (Scala)

The load-bearing step is the dispatch permutation: group token indices by destination expert, truncate each group at the capacity bound, and record which tokens were dropped so the combine step can fall back to the residual path.

```scala
final case class Dispatch(
    byExpert: Map[Int, Vector[Int]], // expert id -> accepted token indices
    dropped: Set[Int]                // tokens no expert accepted
)

/** topK(t) = the experts token t routed to, in gate order. */
def dispatch(
    topK: Vector[Vector[Int]],
    numExperts: Int,
    capacityFactor: Double
): Dispatch =
  val capacity = math.ceil(capacityFactor * topK.size / numExperts).toInt

  // Token order is the arbitration rule: the first arrivals fill the buffer.
  val (accepted, _) =
    topK.zipWithIndex.foldLeft((Map.empty[Int, Vector[Int]], Map.empty[Int, Int])):
      case ((acc, cnt), (experts, token)) =>
        experts.foldLeft((acc, cnt)):
          case ((a, c), e) =>
            val used = c.getOrElse(e, 0)
            if used >= capacity then (a, c) // over capacity: this token is refused
            else (a.updated(e, a.getOrElse(e, Vector.empty) :+ token), c.updated(e, used + 1))

  val placed = accepted.values.flatten.toSet
  Dispatch(accepted, topK.indices.toSet -- placed)

/** Per-device load after sharding experts contiguously; the step time tracks the max. */
def deviceLoad(d: Dispatch, numExperts: Int, devices: Int): Vector[Int] =
  val perDevice = numExperts / devices
  val byDevice: Map[Int, Int] =
    d.byExpert.groupMapReduce((e, _) => e / perDevice)((_, ts) => ts.size)(_ + _)
  // Devices that received nothing must still appear, or the max reads off the wrong slot.
  Vector.tabulate(devices)(dev => byDevice.getOrElse(dev, 0))
```

`deviceLoad` states the constraint numerically: the step cost is the maximum of that vector, not its mean, and EPLB exists to reduce the gap between the two.

## Deployment surface

Both major serving stacks expose EP as a deployment mode. The following serves DeepSeek-V3 under vLLM across 8 GPUs, with data parallelism for attention and expert parallelism for the feed-forward networks:

```bash
vllm serve deepseek-ai/DeepSeek-V3 \
    --tensor-parallel-size 1 \
    --data-parallel-size 8 \
    --enable-expert-parallel \
    --all2all-backend deepep_low_latency \
    --enable-eplb \
    --eplb-config '{"window_size":1000,"step_interval":3000,"num_redundant_experts":2}'
```

SGLang exposes the same building blocks under different names: `--ep-size` sets the expert-parallel degree, `--moe-a2a-backend deepep` selects the DeepEP dispatch and combine kernels, `--deepep-mode` picks `normal` for prefill or `low_latency` for decode, and `--enable-eplb` activates the load balancer. Both stacks additionally offer a mode that overlaps the collective with computation across two micro-batches — SGLang names it two-batch overlap — so the dispatch of one micro-batch proceeds under the expert compute of another.

## Pitfalls

- **Reading a stable loss curve as evidence of balanced routing.** Dropped tokens still produce output via the residual connection, so quality degrades gradually rather than failing; only per-expert token counts reveal the skew.
- **Raising the capacity factor until drops disappear.** The padded slots are transmitted and computed like real tokens, so the interconnect and the expert matmuls absorb work for entries that carry nothing.
- **Attributing an idle-GPU profile to the network.** Under EP the all-to-all is a barrier, so a single overloaded expert leaves every other device waiting; the idle time appears at the collective while its cause is the routing distribution.
- **Using the prefill communication mode for decode.** DeepEP separates a high-throughput mode from a low-latency mode; decode issues small, frequent collectives whose cost is dominated by latency rather than bandwidth.
- **Assuming an auxiliary load-balancing loss fixes serving skew.** That loss shapes the router during training against the training distribution; a production request mix can concentrate load on different experts, which is what EPLB measures at runtime.
- **Confusing expert parallelism with tensor parallelism when sizing devices.** EP requires each expert to fit whole on its device, so an expert larger than one GPU's memory cannot be placed by EP alone.
