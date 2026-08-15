---
title: "Speculative decoding: a cheap draft model that accelerates a large model without changing its output"
date: 2026-07-31
track: sys-patterns
summary: "Large language model generation emits one token per forward pass, and a large model's pass is memory-bandwidth-bound. Speculative decoding has a small draft model propose several tokens ahead and verifies them in a single large-model pass, accepting a variable-length prefix while provably preserving the large model's output distribution. Reported end-to-end latency gains cluster around 2–3×, at the cost of a second model in memory and a delicate accept/resample path."
reading_time: 6
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
  - title: "Cai et al. — Medusa: Simple LLM Inference Acceleration Framework with Multiple Decoding Heads (arXiv:2401.10774)"
    url: "https://arxiv.org/abs/2401.10774"
---

**Gist.** Autoregressive decoding in a large language model (LLM) produces one token per forward pass, and each pass of a large model streams the entire weight set through the accelerator, so the step is bound by memory bandwidth rather than arithmetic. Speculative decoding fills that idle arithmetic capacity: a small **draft** model proposes `k` tokens, one **target** model pass scores all `k` positions at once, and a modified rejection-sampling rule accepts a prefix while leaving the emitted distribution identical to the target's. The cost is a second set of weights resident in memory, a per-step drafting overhead that is wasted whenever the draft is rejected early, and an acceptance-rate dependence that can turn the technique into a net loss under large batches.

## The structural bottleneck

To produce token *N+1* the model must have produced token *N*. A 70-billion-parameter model therefore emits text one forward pass at a time, and each pass reads a multi-gigabyte weight set to compute a single token. The arithmetic units are largely idle during that transfer: the step is **memory-bandwidth-bound**, and its duration is governed by weight movement rather than by the number of multiply-accumulate operations performed. The key observation is that scoring *several* positions in one pass costs approximately the same wall-clock time as scoring one, because the weights are read once either way.

## The pattern

Two models participate: a small, fast **draft** model and the large **target** model whose output quality is required.

1. **Draft.** The small model autoregressively proposes a run of `k` tokens, where `k` is a small fixed lookahead. The run is cheap because the drafting model's weight set is small.
2. **Verify in one pass.** All `k` proposed tokens are appended to the prefix and fed to the *target* model in a **single forward pass**. Since the candidate tokens are already laid out as a sequence, the target computes its own next-token distribution for each of those positions in parallel — one expensive pass scores `k` candidates instead of one.
3. **Accept a prefix.** The proposals are walked left to right and each is accepted with a probability derived from comparing the draft and target distributions. The longest agreeing prefix is accepted; the walk **stops at the first rejection**, and that position is resampled from a corrected distribution.

The invariant that makes this a serving pattern rather than a heuristic: **every step emits at least one token**. On a fully accepted step, `k` tokens (plus a bonus token drawn from the target distribution at the final position, which the same pass already computed) are produced for the price of one target pass. On a step rejected at position 0, one correct token is still produced by the resample. Progress is monotone; throughput rises with the draft's acceptance rate.

## Why the output is provably unchanged

The property that permits enabling this in production is **distribution preservation**: the emitted sequence is distributed exactly as if the target model had generated every token itself. This is not an approximation — it is the same distribution, established in Leviathan et al. and Chen et al. via a modified rejection-sampling acceptance rule.

For a proposed token `x`, with `q` the draft's probability and `p` the target's:

```text
accept x with probability  min(1, p(x) / q(x))

on rejection, sample a replacement from the residual:
    p_resid(x) = normalize( max(0, p(x) - q(x)) )
```

Where the draft is overconfident relative to the target (`q(x) > p(x)`), the token is rejected with probability `1 − p(x)/q(x)`. The replacement is then drawn from `p_resid`, which carries precisely the probability mass the target assigns and the draft under-weights. Summing the two paths by which a token can be emitted — accepted after being proposed, or drawn from the residual after a rejection — yields `p`, the target's own distribution. Two consequences follow. First, correctness does not depend on the draft's quality at all: an arbitrarily bad draft degrades speed, never output. Second, the residual must be renormalised, since `max(0, p − q)` is sub-normalised whenever any acceptance is possible.

**Everything after the first rejection is discarded.** Draft tokens at positions beyond the rejection were conditioned on a token that was not emitted, so their target scores are void and the corresponding work is wasted.

## Draft selection governs the speedup

Speedup is governed by the expected number of tokens emitted per step divided by the cost of that step — the drafting passes plus one verify pass. Two failure modes bound it:

- **Draft too weak.** Acceptance is low, rejections are frequent, and the drafting work is not repaid. A draft disagreeing with the target on half of positions contributes little.
- **Draft too strong, therefore slow.** Acceptance is high but drafting itself consumes a significant fraction of a target pass, absorbing the gain.

The usable region is a model orders of magnitude smaller than the target and sharing its tokenizer. Approaches differ mainly in *where the draft originates*: a separate small model (the classic formulation of Leviathan et al. and Chen et al.), extra decoding heads attached to the target itself, whose candidate continuations are verified together as a tree (**Medusa**), or an n-gram / prompt-lookup draft that copies likely continuations out of the context, which is effective where the output echoes the input. Reported end-to-end gains cluster around **2–3×** lower latency, and mainstream serving stacks including vLLM expose the feature as configuration.

## When it does not pay

Speculative decoding targets **latency-bound, low-batch** serving, where a single request occupies the accelerator. Under **large batch sizes** the accelerator is already compute-saturated — many sequences share one weight read — so the memory-bandwidth slack the technique exploits has been consumed by batching, and the added draft and verify overhead can make throughput worse. It also costs memory (a second model or extra heads) and implementation complexity concentrated in the accept/resample path. The acceptance rate must be measured on representative traffic before committing.

### Implementation sketch (Scala)

```scala
type Dist = IndexedSeq[Double]           // per-token probabilities

def residual(p: Dist, q: Dist): Dist =
  val diff = p.lazyZip(q).map((pi, qi) => math.max(0.0, pi - qi))
  val z = diff.sum                       // sub-normalised; renormalise or the draw is invalid
  diff.map(_ / z)

def sample(d: Dist, rng: scala.util.Random): Int =
  val u = rng.nextDouble()
  val i = d.scanLeft(0.0)(_ + _).tail.indexWhere(_ >= u)
  if i >= 0 then i else d.length - 1     // cumulative sum can fall short of u by rounding

/** One speculative step: draftToks are the k proposals, q(i) and p(i) the draft
  * and target distributions at position i. p has k+1 entries: the extra one is
  * the bonus position the verify pass already scored. */
def step(draftToks: IndexedSeq[Int], q: IndexedSeq[Dist], p: IndexedSeq[Dist],
         rng: scala.util.Random): IndexedSeq[Int] =
  val accepted = IndexedSeq.newBuilder[Int]
  var i = 0
  while i < draftToks.length do
    val tok = draftToks(i)
    if rng.nextDouble() < math.min(1.0, p(i)(tok) / q(i)(tok)) then
      accepted += tok
      i += 1
    else
      accepted += sample(residual(p(i), q(i)), rng)
      return accepted.result()           // suffix is void: it was conditioned on a dropped token
  accepted += sample(p(draftToks.length), rng)
  accepted.result()
```

## Pitfalls

- **Mismatched tokenizers.** If draft and target segment text differently, position `i` in the draft does not correspond to position `i` in the target, and the ratio `p(x)/q(x)` compares unrelated events; output is corrupted rather than merely slow.
- **Forgetting to renormalise the residual.** `max(0, p − q)` sums to less than 1, so sampling from it directly biases toward low indices or falls off the end of the cumulative array.
- **Continuing past the first rejection.** Accepting later draft tokens after a rejection breaks distribution preservation, because those tokens were conditioned on a prefix that was never emitted.
- **Tuning `k` upward without watching acceptance.** Larger `k` increases drafting cost linearly while the expected accepted length saturates at the acceptance rate's limit, so latency regresses past a traffic-dependent point.
- **Benchmarking at batch size 1 and deploying under load.** The measured gain evaporates once batching saturates the accelerator's arithmetic units, and the drafting overhead remains.
- **Treating acceptance rate as a property of the model pair.** It is a property of the pair *and the prompt distribution*: a draft that performs well on code completion may perform poorly on open-ended dialogue.
