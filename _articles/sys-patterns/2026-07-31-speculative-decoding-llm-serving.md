---
title: "Speculative decoding: a cheap draft model to make a big model faster without changing its output"
date: 2026-07-31
track: sys-patterns
summary: "LLM generation is one token per forward pass, and a big model's pass is expensive and memory-bound. Speculative decoding lets a tiny draft model guess several tokens ahead, then verifies them all in a single big-model pass — accepting a variable number per step while provably keeping the big model's exact output distribution. 2–3× lower latency for free correctness."
reading_time: 5
tags: [speculative-decoding, llm, inference, serving, ai-infrastructure, vllm]
sources:
  - title: "Leviathan, Kalman, Matias — Fast Inference from Transformers via Speculative Decoding (ICML 2023, arXiv:2211.17192)"
    url: "https://arxiv.org/abs/2211.17192"
  - title: "Chen et al. (DeepMind) — Accelerating LLM Decoding with Speculative Sampling (arXiv:2302.01318)"
    url: "https://arxiv.org/abs/2302.01318"
  - title: "NVIDIA Technical Blog — An Introduction to Speculative Decoding for Reducing Latency in AI Inference"
    url: "https://developer.nvidia.com/blog/an-introduction-to-speculative-decoding-for-reducing-latency-in-ai-inference/"
  - title: "vLLM documentation — Speculative Decoding"
    url: "https://docs.vllm.ai/en/latest/features/spec_decode.html"
  - title: "Cai et al. — Medusa: Simple LLM Inference Acceleration Framework (arXiv:2401.10774)"
    url: "https://arxiv.org/abs/2401.10774"
---

Autoregressive generation has a structural bottleneck: to produce token *N+1* you must have produced token *N*, so a 70B model emits text one forward pass at a time. Each of those passes is **memory-bandwidth-bound** — you stream the entire multi-gigabyte weight set through the accelerator to compute a single token. The compute units are mostly idle; you are paying for weight movement, not math. That imbalance is exactly what speculative decoding exploits.

## The pattern

Run two models: a small, fast **draft** model and the large **target** model whose quality you actually want.

1. **Draft.** The small model autoregressively proposes a short run of `k` tokens (say 4). This is cheap because the model is small.
2. **Verify in one pass.** Feed all `k` proposed tokens to the *target* model in a **single forward pass**. Because the tokens are already laid out, the target computes its own probability for each position in parallel — one expensive pass scores four candidate tokens instead of one.
3. **Accept a prefix.** Walk the proposals left to right and accept each token with a probability derived from comparing draft and target distributions. Accept the longest agreeing prefix; **reject at the first disagreement** and resample that position from a corrected distribution.

The move that makes this serving pattern rather than a hack: on a good step you got `k` tokens for the price of *one* target pass. On a bad step you still got at least one correct token (the resampled one), so you never go backward. Throughput rises with the draft's acceptance rate.

## Why the output is provably unchanged

The property that makes speculative decoding safe to turn on in production is **distribution preservation**: the sequence you emit is distributed *exactly* as if the target model had generated every token itself. This is not "close enough" — it is the same distribution, proven in the original Leviathan et al. and Chen et al. papers via a modified-rejection-sampling acceptance rule.

The acceptance test for a proposed token `x`, where `q` is the draft's probability and `p` the target's:

```text
accept x with probability  min(1, p(x) / q(x))

on rejection, sample a replacement from the residual:
    p_resid(x) = normalize( max(0, p(x) - q(x)) )
```

When the draft was overconfident relative to the target (`q(x) > p(x)`) you sometimes reject; when you reject you resample from `p_resid`, which is precisely the mass the target has that the draft under-weighted. Do the algebra and the net distribution over emitted tokens equals `p` — the target's own distribution. So you can enable it on a model serving real users and change nothing observable except latency.

## Choosing the draft matters more than anything

Speedup ≈ (tokens accepted per step) ÷ (cost of drafting + one verify). Two failure modes bound it:

- **Draft too weak:** low acceptance, you reject constantly, and the extra drafting work isn't repaid. A draft that disagrees with the target half the time barely helps.
- **Draft too strong (slow):** high acceptance but drafting itself is expensive, eating the win.

The sweet spot is a small model from the same family/tokenizer as the target — e.g. a 1B draft for a 70B target. Approaches differ mainly in *where the draft comes from*: a separate small model (classic), extra decoding heads bolted onto the target (**Medusa**), tree-structured drafts verified together (**EAGLE**), or even a plain **n-gram / prompt-lookup** draft that just copies likely continuations from the context — surprisingly effective for summarization and code where output echoes input. Reported end-to-end gains cluster around **2–3×** lower latency, and mainstream serving stacks like vLLM and TensorRT-LLM expose it as a config flag.

## When it doesn't pay

Speculative decoding helps **latency-bound, low-batch** serving — one user waiting on a response. Under **large batch sizes** the accelerator is already compute-saturated (many sequences filling the pass), the memory-bandwidth slack it exploits is gone, and the extra draft/verify overhead can *lose*. It also adds memory (a second model or extra heads) and real implementation complexity in the accept/resample path. Measure acceptance rate on *your* traffic before committing.

## Sketch

```python
def spec_decode_step(target, draft, prefix, k):
    # 1. draft proposes k tokens autoregressively (cheap, small model)
    draft_toks, q = draft.generate(prefix, k)      # q: draft probs
    # 2. ONE target pass scores all k positions at once
    p = target.forward(prefix + draft_toks)        # p: target probs per pos
    out = []
    for i, tok in enumerate(draft_toks):
        if random() < min(1.0, p[i][tok] / q[i][tok]):
            out.append(tok)                        # accept
        else:
            out.append(sample(residual(p[i], q[i])))  # reject + correct
            return out                             # stop at first reject
    out.append(sample(p[k]))                       # bonus token from the pass
    return out
```

**Try next:** stand up any small+large model pair from the same family in vLLM with `--speculative-config` (draft model + `num_speculative_tokens`), then log the *acceptance rate* on a batch of your real prompts. If it's above ~60% you'll see the latency drop; if it's low, swap the draft — that single number, not the model size, is what governs the speedup.
