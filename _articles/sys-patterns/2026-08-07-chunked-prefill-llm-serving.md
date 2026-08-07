---
title: "Chunked Prefill: Slicing Long Prompts So Decode Never Stalls"
date: 2026-08-07
track: sys-patterns
summary: "A long prompt's prefill hogs the GPU and freezes everyone else's decode. Chunked prefill splits that prefill into token-budget-sized slices and piggybacks them onto decode steps in one engine iteration, trading a little TTFT for far smoother inter-token latency."
reading_time: 6
tags: [llm-serving, chunked-prefill, inference, vllm, ai-infrastructure]
sources:
  - title: "Taming Throughput-Latency Tradeoff in LLM Inference with Sarathi-Serve (OSDI '24)"
    url: "https://www.usenix.org/system/files/osdi24-agrawal.pdf"
  - title: "Optimization and Tuning — vLLM documentation"
    url: "https://docs.vllm.ai/en/stable/configuration/optimization/"
  - title: "vLLM V1 — the unified scheduler"
    url: "https://docs.vllm.ai/en/v0.9.2/usage/v1_guide.html"
  - title: "5 steps to triage vLLM performance (Red Hat Developer)"
    url: "https://developers.redhat.com/articles/2026/03/09/5-steps-triage-vllm-performance"
  - title: "vLLM V1 performance optimization (AMD ROCm)"
    url: "https://rocm.docs.amd.com/en/latest/how-to/rocm-for-ai/inference-optimization/vllm-optimization.html"
---

A single 8,000-token prompt lands on your serving GPU. For the next few hundred milliseconds, every other user's token generation freezes. Their streams stutter, your p99 inter-token latency spikes, and dashboards light up — all because one request is doing its prefill. Chunked prefill is the scheduling trick that stops this, and since the vLLM V1 engine it is on by default whether you asked for it or not.

The disaggregation pattern solves the same prefill-vs-decode tension by pushing the two phases onto *separate* GPU pools; chunked prefill takes the opposite philosophy and interleaves them inside **one** engine step on **one** GPU.

## The prefill/decode tension

An LLM request has two phases with opposite hardware appetites. **Prefill** ingests the whole prompt in one parallel forward pass — thousands of tokens hit the matmul units at once, so it is **compute-bound** and saturates the GPU's FLOPs. **Decode** then emits one token per step, and each step streams the entire model weights and KV cache out of HBM to produce a single token — so it is **memory-bandwidth-bound**, leaving the compute units mostly idle.

The Sarathi-Serve paper (Agrawal et al., OSDI '24), which introduced chunked prefill, quantifies the asymmetry bluntly: batching "boosts decode phase throughput immensely but has little effect on prefill throughput," because decode runs in a "memory-bound regime leaving compute underutilized." That idle compute during decode is *slack* — and slack is an opportunity.

The naive fix is hybrid batching: throw a prefill into the same batch as your running decodes. But a full prefill is enormous relative to a decode step, so it dominates the iteration's runtime. Every decode sharing that batch waits for the whole prefill to finish. Sarathi-Serve measured this as a spike of **up to 28.3x in time-between-tokens (TBT) latency**. You have protected throughput and wrecked your ITL SLO.

## Slicing prefill under a token budget

Chunked prefill's insight: you do not have to run a prefill all at once. Split the prompt into "near equal sized chunks" and feed one chunk per iteration. Now define a single `token_budget` — the maximum number of tokens any one engine step may process. The scheduler fills each step in priority order:

```
   one engine step  (token budget = 2048)
   ┌──────────────────────────────────────────────────┐
   │ decode  decode  decode ... decode │ prefill chunk  │
   │  R1      R2      R3         R30    │  of R31        │
   │  1 tok   1 tok   1 tok      1 tok  │  ~2018 tokens  │
   └──────────────────────────────────────────────────┘
     ^ 30 running decodes piggyback for free   ^ prompt sliced
       (compute slack)                           to fit remainder
```

Every step first packs all in-flight decode tokens (one per running sequence — cheap, memory-bound), then spends the **remaining** budget on a slice of some prefill. Because decode leaves the compute units hungry, "more tokens can be processed along with a decode batch without significantly increasing its latency" — the prefill chunk rides along on slack that was being wasted. Sarathi-Serve calls the decodes **piggybacking** on the prefill chunk, and the resulting schedule **stall-free**: "By restricting the computational load in every iteration, stall-free batching ensures that decodes never experience a generation stall."

A 16,000-token prompt no longer blocks the world for one giant iteration. It occupies a slice of ~8 consecutive steps, and between each slice the running decodes keep ticking. The long prefill is amortized instead of monopolizing.

## The throughput/latency trade-off

Chunked prefill is not free lunch — it is a dial. The token budget sets where you sit on the throughput-vs-latency curve:

- **Smaller budget** → prefill is cut into more, smaller chunks → decodes interrupted less often per step → **lower, smoother ITL**, but the prompt takes more steps to finish → **higher TTFT**, and more per-step overhead → lower peak throughput.
- **Larger budget** → bigger prefill chunks → prompt finishes in fewer steps → **lower TTFT and higher throughput**, but each step's prefill is chunkier and jostles the piggybacking decodes → **higher ITL**.

There is also a real cost: slicing a prefill means later chunks re-read the KV cache of earlier chunks from HBM, so total prefill compute rises slightly versus a single pass. That is the price of smoothness. Sarathi-Serve still nets large capacity wins because stall-free batching lets you pack the GPU harder without blowing the latency SLO — **2.6x** higher serving capacity on Mistral-7B, up to **3.7x** on Yi-34B, and up to **5.6x** on Falcon-180B under tight SLOs.

## How vLLM exposes it

In the legacy **V0** engine, chunked prefill was opt-in via `--enable-chunked-prefill`, with `max_num_batched_tokens` — the token budget — defaulting to **2048**. Smaller values "achieve better ITL because there are fewer prefills interrupting decodes"; the docs explicitly recommend `max_num_batched_tokens > 2048` when you are optimizing for throughput.

In the **V1** engine, chunked prefill is **enabled by default** and you generally do not turn it off. V1 replaced the old two-phase scheduler with a unified one: it "treats both prompt and output tokens the same way by using a simple dictionary (e.g., `{request_id: num_tokens}`) to dynamically allocate a fixed token budget per request... without a strict separation between prefill and decode phases." Prefill chunking, prefix caching, and speculative decoding all fall out of that one abstraction. The V1 default budget is larger — **8192 for online serving, 16384 for offline** — tuned for throughput now that chunking is universal.

Setting the dial explicitly:

```python
from vllm import LLM

llm = LLM(
    model="meta-llama/Llama-3.1-8B-Instruct",
    enable_chunked_prefill=True,   # default in V1; explicit here for clarity
    max_num_batched_tokens=2048,   # the token budget — lower = smoother ITL
    max_num_seqs=256,
)
```

```bash
# Equivalent on the server, tuned for interactive/streaming latency
vllm serve meta-llama/Llama-3.1-8B-Instruct \
  --max-num-batched-tokens 2048 \
  --max-num-seqs 256
```

**Tuning heuristic for `max_num_batched_tokens`:** treat it as your ITL-vs-throughput knob. For interactive chat where smooth streaming matters, keep it small — **2k for tight ITL, up to ~8k–16k** for a balance of low TTFT and decent throughput. For offline or batch jobs where you only care about tokens/sec, push it high — **16k–32k and up**, near `--max-model-len`, accepting that returns diminish past ~32k. If you see periodic ITL spikes in production that line up exactly with long prompts arriving, that is chunked prefill working — and a signal to *lower* the budget, not raise it (Red Hat's vLLM triage guide flags this exact symptom).

The mental model: disaggregation *separates* prefill and decode in space (different GPUs); chunked prefill *interleaves* them in time (one GPU, one step, one budget). Small deployments and single nodes usually reach for chunked prefill first — it needs no interconnect, no second pool, just a well-chosen number.

**Try next:** Serve an 8B model with `vllm bench serve` on a mixed 4000-in / 200-out workload, sweep `--max-num-batched-tokens` across 512, 2048, 8192, and 32768, and plot p99 ITL against total throughput. Watch the two metrics move in opposite directions — that curve *is* the chunked-prefill trade-off, and picking your operating point on it is the whole job.
