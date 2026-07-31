---
title: "Expert parallelism: serving an MoE model when the experts don't fit on one GPU"
date: 2026-07-31
track: sys-patterns
summary: "A Mixture-of-Experts LLM routes each token to a few of hundreds of experts, so the weights are enormous but only a slice runs per token. Serving that means scattering experts across GPUs and shuffling tokens to wherever their experts live — an all-to-all on every layer. This article covers the expert-parallel placement pattern, the dispatch/combine communication, and why load imbalance (not FLOPs) is the thing that will wreck your throughput."
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

A dense LLM runs every parameter for every token. A **Mixture-of-Experts (MoE)** model does not: each MoE layer holds many parallel feed-forward "experts," and a small **router** sends each token to only its top-k of them (Switch Transformer uses top-1, GShard top-2). The result is a model with, say, 256 experts per layer — hundreds of billions of parameters total — that still spends dense-model FLOPs per token. Great for training economics. A headache for serving, because those experts are far too big to replicate on every GPU, and *which* expert a token needs isn't known until the router fires at runtime.

## The placement pattern: experts across GPUs

The serving answer is **expert parallelism (EP)**: shard the experts of each MoE layer across GPUs, so GPU 0 holds experts 0–31, GPU 1 holds 32–63, and so on. Attention and the non-expert layers are replicated (usually via tensor or data parallelism); only the expert FFNs are partitioned. This is distinct from tensor parallelism, which splits *every* weight matrix across GPUs — EP keeps each expert whole and instead distributes *which* experts live where.

That placement creates a routing problem on every single MoE layer. A token processed on GPU 0 might be routed to an expert on GPU 3. So each layer becomes a two-step shuffle:

1. **Dispatch (all-to-all):** every GPU sends each of its tokens to the GPU that owns the token's chosen expert(s).
2. **Combine (all-to-all):** after the experts run, results are shuffled back so each token returns to the GPU that owns its sequence, weighted by the router's gate values.

Two all-to-all collectives per MoE layer, per forward pass. On a many-layer model that is a lot of network traffic sitting directly on the critical path, which is why the communication kernel matters as much as the matmul. DeepSeek's **DeepEP** exists precisely for this: a library of tuned all-to-all dispatch/combine kernels with a high-throughput mode for prefill and a low-latency mode for decode. vLLM and SGLang both plug it in as a backend.

## The real enemy: load imbalance

Here's the pattern's sharp edge. FLOPs are fixed and predictable; *routing* is not. Nothing forces the router to spread tokens evenly, and in practice it doesn't — some experts become "hot" and receive far more tokens than others. Under expert parallelism, a hot expert means a hot *GPU*, and because the all-to-all is a barrier, **every GPU waits for the slowest one.** Idle silicon, throttled throughput.

Frameworks bound this with a **capacity factor**: each expert gets a fixed buffer of `capacity = capacity_factor × (tokens / num_experts)` slots. It keeps the collective a fixed, rectangular shape (essential for efficient kernels), but it forces a choice. Tokens that overflow a hot expert's capacity are **dropped** — they skip the expert entirely and pass through via the residual connection, degrading quality. Set the capacity factor too low and you drop tokens; too high and you pad and waste compute and bandwidth. The Expert-Choice routing work noted over-capacity ratios of 20–40% for some experts under naive token-choice routing — that's how skewed real traffic gets.

Two mitigations layer on top. During training, an **auxiliary load-balancing loss** nudges the router toward uniform expert usage. At serving time, an **expert-parallel load balancer (EPLB)** rebalances *placement*: it measures per-expert load and either moves experts or replicates hot ones onto extra GPUs (redundant experts) so no single device is the bottleneck.

## Turning it on

Both major serving stacks expose EP as a deployment mode. vLLM, serving DeepSeek-V3 across 8 GPUs with data parallelism for attention and expert parallelism for the FFNs:

```bash
vllm serve deepseek-ai/DeepSeek-V3 \
    --tensor-parallel-size 1 \
    --data-parallel-size 8 \
    --enable-expert-parallel \
    --all2all-backend deepep_low_latency \
    --enable-eplb \
    --eplb-config '{"window_size":1000,"step_interval":3000,"num_redundant_experts":2}'
```

SGLang exposes the same building blocks with slightly different names — `--ep-size` sets the expert-parallel degree, `--moe-a2a-backend deepep` selects the DeepEP dispatch/combine kernels, `--deepep-mode` picks `normal` (prefill) vs `low_latency` (decode), and `--enable-eplb` turns on the load balancer. Both stacks also overlap the all-to-all with compute (vLLM/SGLang "two-batch overlap") so the dispatch of one micro-batch hides under the expert compute of another — the standard way to keep the collective off the critical path.

**Try next:** serve a small MoE (e.g. a Qwen or DeepSeek MoE) with `--enable-expert-parallel` on 2–4 GPUs, then log per-expert token counts for a few hundred requests. Plot the histogram, sweep the capacity factor down until you see dropped tokens, then switch on EPLB and watch the hot-GPU tail flatten — that gap between mean and max expert load is exactly what expert parallelism has to fight.
