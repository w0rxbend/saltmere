---
title: "Prefix Caching: the KV-reuse pattern for LLM serving"
date: 2026-07-31
track: sys-patterns
summary: "Treat the shared front of every prompt as a cacheable resource. Prefix caching reuses KV state across requests, and like continuous batching it's a serving pattern you configure, not a model you retrain."
reading_time: 5
tags: [llm-serving, vllm, sglang, kv-cache, caching, inference, distributed-systems]
sources:
  - title: "Automatic Prefix Caching — vLLM docs"
    url: "https://docs.vllm.ai/en/latest/features/automatic_prefix_caching/"
  - title: "Automatic Prefix Caching (design) — vLLM docs"
    url: "https://docs.vllm.ai/en/stable/design/prefix_caching/"
  - title: "Fast and Expressive LLM Inference with RadixAttention and SGLang — LMSYS"
    url: "https://www.lmsys.org/blog/2024-01-17-sglang/"
  - title: "SGLang: Efficient Execution of Structured Language Model Programs (arXiv:2312.07104)"
    url: "https://arxiv.org/abs/2312.07104"
  - title: "Performance boosts in vLLM 0.8.1: switching to the V1 engine — Red Hat Developer"
    url: "https://developers.redhat.com/articles/2025/04/28/performance-boosts-vllm-081-switching-v1-engine"
---

Every request to a chat model carries the same freight up front: a system prompt, a tool schema, a handful of few-shot examples. The transformer recomputes the attention key/value tensors for all of it, every time, before it emits a single new token. That prefill is pure waste when the prefix is identical to the last thousand requests.

**Prefix caching** is the serving pattern that reclaims it. Compute the KV cache for a token span once, keep it in GPU memory keyed by the token content, and reuse it for any later request that starts with the same tokens. It sits alongside continuous batching and speculative decoding in the same family Brendan Burns would recognize from *Designing Distributed Systems*: a reusable, infrastructure-level pattern that sits between the client and the accelerator, transparent to the model.

## Why it works

KV cache is already **paged**. PagedAttention (vLLM) and its relatives store the cache in fixed-size blocks of tokens rather than one contiguous buffer per sequence. Once your KV state is block-structured, prefix reuse is almost free: hash the tokens in each block, and if a request's leading blocks hash to blocks you already hold, you point the new sequence at them instead of recomputing.

The lever you care about is the **cache hit rate** — the fraction of prefill tokens served from cache. It's driven entirely by prefix sharing:

- One shared system prompt across all traffic — a long, static prefix hits on nearly every request.
- Few-shot exemplars pinned ahead of the user turn — shared across a whole workload.
- Multi-turn chat — turn N reuses the KV of turns 1..N-1 for the same conversation.
- RAG where many queries share a boilerplate instruction header but differ in retrieved chunks — you get the header hit but not the documents.

The corollary is a real design constraint: **put the variable part last**. A per-request timestamp or user ID spliced into the top of your system prompt poisons every downstream block and drops your hit rate to zero. Sort your prompt from most-shared to least-shared.

## Turning it on in vLLM

In the V1 engine — the default since vLLM 0.8.1 (April 2025), and still so as of the 0.22.x line in mid-2026 — automatic prefix caching is **on by default**. You disable it explicitly rather than opt in:

```bash
# APC is already enabled on V1; this is the explicit form
vllm serve meta-llama/Llama-3.1-8B-Instruct \
    --enable-prefix-caching

# to turn it off
vllm serve meta-llama/Llama-3.1-8B-Instruct \
    --no-enable-prefix-caching
```

Two knobs matter. `--block-size` (default 16 tokens on CUDA) sets the granularity of a cacheable unit — sharing is quantized to block boundaries, so a prefix that diverges mid-block only shares up to the last full common block. `--prefix-caching-hash-algo` chooses how blocks are keyed: `builtin` (Python's hash, the default) or `sha256` (collision-resistant, at some CPU cost) if you're serving multiple tenants and worry about hash collisions leaking cache across them.

Eviction is **LRU** over the block pool. When GPU memory for KV fills, the least-recently-used blocks are freed first, which naturally keeps hot shared prefixes resident while cold one-off conversations age out.

## RadixAttention: the same idea, generalized

SGLang's **RadixAttention** (Zheng et al., *SGLang*, arXiv:2312.07104; LMSYS blog, Jan 17 2024) pushes prefix reuse from "leading blocks" to "any shared prefix in a tree." It maintains a **radix tree** whose edges are token sequences and whose nodes map to KV cache tensors. A new request walks the tree, matches the longest shared prefix — even one branching off a conversation three turns deep — and reuses it, with the same LRU leaf eviction.

The extra piece is **cache-aware scheduling**: reorder the batch so requests that share a prefix run together, maximizing hits before the shared node gets evicted. SGLang reported up to ~5x throughput over prior systems on shared-prefix workloads, and up to 6.4x in the paper's structured-program benchmarks. For agent loops and tree-of-thought search — where many branches share a long common history — this tree structure buys far more than flat leading-prefix caching.

## Where it fits

Prefix caching composes cleanly with the other serving patterns. Continuous batching keeps the accelerator busy across requests; prefix caching removes redundant prefill *within* those requests; speculative decoding attacks the decode phase. None of them touch model weights, and you can run all three at once. That's the point of treating them as patterns: they're orthogonal layers of the serving stack, each configurable, each measurable.

**Try next:** Serve a model with `--enable-prefix-caching`, fire the same 400-token system prompt twice with `temperature=0`, and compare the reported prefill/time-to-first-token on request two — then move a per-request ID to the top of the prompt and watch the second-request speedup vanish.
