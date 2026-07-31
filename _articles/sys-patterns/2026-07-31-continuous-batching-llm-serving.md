---
title: "Continuous Batching: Keeping the GPU Full Between Tokens"
date: 2026-07-31
track: sys-patterns
summary: "Why static batching strands GPU cycles when sequences finish early, and how iteration-level scheduling plus PagedAttention swap requests in and out mid-flight for up to 23x throughput."
reading_time: 5
tags: [llm, inference, batching, vllm, gpu, serving]
sources:
  - title: "Orca: A Distributed Serving System for Transformer-Based Generative Models (OSDI '22)"
    url: "https://www.usenix.org/system/files/osdi22-yu.pdf"
  - title: "Efficient Memory Management for Large Language Model Serving with PagedAttention (SOSP '23)"
    url: "https://arxiv.org/pdf/2309.06180"
  - title: "How continuous batching enables 23x throughput in LLM inference (Anyscale)"
    url: "https://www.anyscale.com/blog/continuous-batching-llm-inference"
  - title: "vLLM — vllm serve CLI reference"
    url: "https://docs.vllm.ai/en/stable/cli/serve/"
  - title: "TensorRT-LLM — In-flight (continuous) batching, gpt-attention docs"
    url: "https://github.com/NVIDIA/TensorRT-LLM/blob/main/docs/source/legacy/advanced/gpt-attention.md"
---

## The problem: sequences don't finish together

An autoregressive LLM generates one token per forward pass. To use the GPU efficiently you batch many requests into one pass — but generation lengths are wildly uneven. One prompt wants 12 tokens, another wants 800.

With **static batching**, you assemble a batch, run it to completion, and only then admit new work. The batch's latency is dictated by its slowest member. As short sequences hit their EOS token, their rows in the batch go idle but stay allocated — you keep launching kernels over dead slots until the last sequence finishes. On skewed workloads the GPU spends much of its time computing padding. This is pure waste of the most expensive resource in the system.

## Iteration-level scheduling

Orca (Yu et al., OSDI 2022) fixed this by moving the scheduling boundary from the *request* to the *iteration*. Instead of "pick a batch, run it to the end," the scheduler decides membership **before every single decode step**. After each token:

- Any sequence that just emitted EOS is evicted and its result returned immediately.
- Waiting requests are admitted into the freed slots.
- The next forward pass runs on the newly composed batch.

This is **continuous** (or in-flight) batching. A request that needs 12 tokens leaves after 12 steps rather than waiting 800. The batch stays dense; GPU occupancy stays high. Orca pairs this with *selective batching* — batching the token-parallel matmuls while handling attention per-sequence, since each request has a different KV history.

## Prefill vs decode

Two phases behave differently. **Prefill** processes the whole prompt in one pass — compute-bound, lots of parallel tokens. **Decode** generates one token at a time — memory-bandwidth-bound, tiny per-step compute. Continuous batching's win comes almost entirely from packing many concurrent *decode* streams so each cheap step amortizes the cost of streaming weights from HBM. Modern schedulers also interleave prefill and decode (chunked prefill) so a long prompt doesn't stall everyone's decode.

## Why PagedAttention makes it practical

Swapping sequences in and out only pays off if you can *fit* them. The KV cache — the per-token keys and values every attention step reads — grows with sequence length and dominates memory. Naive serving pre-reserves a contiguous slab per request sized for the max length. The vLLM paper (SOSP 2023) measured that prior systems used only **20.4%–38.2%** of KV cache memory for actual token state; the rest was reserved-but-unused, internal, and external fragmentation.

**PagedAttention** treats the KV cache like OS virtual memory: fixed-size *blocks* mapped through a per-request block table, not necessarily contiguous in physical memory. Blocks are allocated on demand as a sequence grows, so fragmentation is bounded to under one block. That freed memory becomes larger effective batch size — the direct fuel for continuous batching. vLLM reports **2–4x** higher throughput than Orca at the same latency; Anyscale's benchmark shows up to **23x** over naive static batching (vs 8x for continuous batching without paged memory).

## Running it

vLLM ships an OpenAI-compatible server:

```bash
vllm serve meta-llama/Llama-3.1-8B-Instruct \
  --gpu-memory-utilization 0.90 \
  --max-num-seqs 256 \
  --max-num-batched-tokens 8192
```

```bash
curl http://localhost:8000/v1/completions \
  -H "Content-Type: application/json" \
  -d '{
        "model": "meta-llama/Llama-3.1-8B-Instruct",
        "prompt": "Explain KV cache paging in one sentence:",
        "max_tokens": 64
      }'
```

The knobs that shape batching:

- `--gpu-memory-utilization` (default 0.9): fraction of VRAM vLLM claims for weights + KV blocks. Higher = more blocks = bigger batches, until you OOM.
- `--max-num-seqs`: ceiling on concurrent sequences per iteration. The scheduler fills up to this many when memory allows.
- `--max-num-batched-tokens`: total tokens (prefill + decode) per step. Governs prefill chunk size and the compute/latency trade-off.

TensorRT-LLM (in-flight batching) and HuggingFace TGI implement the same iteration-level pattern; the ideas are now table stakes for production serving.

**Try next:** benchmark your own model with `vllm bench serve` at rising request rates, watch where p50 latency stays flat while throughput climbs, then halve `--max-num-batched-tokens` and observe prefill chunking trade latency for smoother decode.
