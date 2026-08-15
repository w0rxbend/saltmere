---
title: "Chunked Prefill: Slicing Long Prompts So Decode Never Stalls"
date: 2026-08-07
track: sys-patterns
summary: "A long prompt's prefill monopolises the GPU and freezes concurrent decode. Chunked prefill splits that prefill into token-budget-sized slices and piggybacks them onto decode steps within one engine iteration, trading time-to-first-token for smoother inter-token latency."
reading_time: 7
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

**Gist.** A large-language-model (LLM) request begins with a prefill pass over the whole prompt, and a long prompt's prefill occupies an engine iteration long enough that every concurrently decoding request misses its turn — Sarathi-Serve measured spikes of **up to 28.3x in time-between-tokens (TBT)** under naive hybrid batching. Chunked prefill caps the work in any single iteration with a **token budget**, slicing each prefill into near-equal chunks so that in-flight decodes ride along in the same step. The cost is paid in time-to-first-token (TTFT) and in slightly higher total prefill compute, because later chunks re-read the key/value (KV) cache written by earlier ones.

Prefill/decode disaggregation resolves the same tension by placing the two phases on *separate* graphics-processing-unit (GPU) pools. Chunked prefill takes the opposite approach and interleaves them inside **one** engine step on **one** GPU.

## The prefill/decode tension

An LLM request has two phases with opposite hardware appetites. **Prefill** ingests the entire prompt in one parallel forward pass — thousands of tokens reach the matrix-multiply units at once, so the phase is **compute-bound** and saturates the GPU's floating-point throughput. **Decode** then emits one token per step, and each step streams the full model weights and KV cache out of high-bandwidth memory (HBM) to produce that single token, so it is **memory-bandwidth-bound** and leaves the compute units largely idle.

The Sarathi-Serve paper (Agrawal et al., OSDI '24), which introduced chunked prefill, states the asymmetry directly: batching "boosts decode phase throughput immensely but has little effect on prefill throughput", because decode runs in a "memory-bound regime leaving compute underutilized". **That idle compute during decode is the slack chunked prefill spends.**

The naive fix is hybrid batching — placing a prefill in the same batch as the running decodes. A full prefill is large relative to a decode step, so it dominates the iteration's runtime and every decode sharing that batch waits for the whole prefill to complete. That is the origin of the 28.3x TBT spike: throughput is protected and the inter-token latency (ITL) objective is destroyed.

## Slicing prefill under a token budget

The mechanism rests on a single invariant: **no engine iteration processes more than `token_budget` tokens in total, counting prompt and output tokens alike.** A prefill is therefore split into "near equal sized chunks" and one chunk is admitted per iteration.

The scheduler fills each step in priority order:

```
   one engine step  (token budget = 2048)
   ┌──────────────────────────────────────────────────┐
   │ decode  decode  decode ... decode │ prefill chunk│
   │  R1      R2      R3         R30   │  of R31      │
   │  1 tok   1 tok   1 tok      1 tok │ ~2018 tokens │
   └──────────────────────────────────────────────────┘
     ^ 30 running decodes admitted first    ^ prompt sliced
       (they consume the memory-bound slack)  to fit remainder
```

Every step first admits all in-flight decode tokens — one per running sequence, cheap and memory-bound — and then spends the **remaining** budget on a slice of some prefill. Because decode leaves the compute units hungry, "more tokens can be processed along with a decode batch without significantly increasing its latency": the prefill chunk occupies slack that would otherwise be wasted. Sarathi-Serve describes the decodes as **piggybacking** on the prefill chunk and the resulting schedule as **stall-free**: "By restricting the computational load in every iteration, stall-free batching ensures that decodes never experience a generation stall."

Under this invariant a 16,000-token prompt no longer blocks the engine for one large iteration. With a 2,048-token budget it occupies a slice of at least eight consecutive steps — more, since the concurrent decodes claim part of each step's budget — and between slices the running decodes continue to advance. The prefill is amortised rather than monopolising.

### Implementation sketch (Scala)

The load-bearing idea is the budget split, not the model execution. A request in prefill carries a cursor into its prompt; a request in decode contributes exactly one token.

```scala
enum Phase:
  case Prefill(consumed: Int, promptLen: Int)
  case Decode

final case class Request(id: String, phase: Phase)

/** One engine iteration: decodes first, remaining budget to one prefill slice. */
def scheduleStep(
    running: List[Request],
    tokenBudget: Int
): (Map[String, Int], List[Request]) =
  val decodes = running.collect { case r @ Request(_, Phase.Decode) => r }
  val plan = decodes.map(r => r.id -> 1).toMap          // invariant: 1 token each
  var remaining = tokenBudget - plan.size

  val advanced = running.map:
    case r @ Request(id, Phase.Prefill(consumed, promptLen)) if remaining > 0 =>
      val slice = math.min(remaining, promptLen - consumed)
      remaining -= slice
      // a prefill that reaches promptLen transitions to Decode on the next step
      if consumed + slice >= promptLen then r.copy(phase = Phase.Decode)
      else r.copy(phase = Phase.Prefill(consumed + slice, promptLen))
    case r => r

  val prefillPlan = advanced.zip(running).collect:
    case (a, Request(id, Phase.Prefill(before, len))) =>
      id -> (a.phase match
        case Phase.Prefill(after, _) => after - before
        case Phase.Decode            => len - before)

  (plan ++ prefillPlan, advanced)
```

Each slice is capped by `remaining`, so prefill never pushes an iteration past `tokenBudget` — the property that bounds iteration time and therefore bounds TBT. If the running decodes alone exhaust the budget, `remaining` is non-positive and no prefill advances at all.

## The throughput/latency trade-off

Chunked prefill is a dial rather than a free improvement. The token budget selects a point on the throughput-versus-latency curve:

- **Smaller budget** → prefill is cut into more, smaller chunks → each step carries less prefill work alongside the decodes → **lower, smoother ITL**, but the prompt requires more steps to finish → **higher TTFT**, and more per-step overhead → lower peak throughput.
- **Larger budget** → larger prefill chunks → the prompt finishes in fewer steps → **lower TTFT and higher throughput**, but each step's prefill work is heavier and lengthens the iteration the piggybacking decodes share → **higher ITL**.

There is also a compute cost: **slicing a prefill means later chunks re-read the KV cache written by earlier chunks from HBM, so total prefill work rises slightly relative to a single pass.** Sarathi-Serve nonetheless reports large capacity gains, because stall-free batching allows the GPU to be packed harder without violating the latency objective: the paper reports serving capacity improved by **up to 2.6x for Mistral-7B on a single A100 GPU** and **up to 5.6x for Falcon-180B on eight A100 GPUs**, measured under tight service-level objectives (SLOs).

## How vLLM exposes it

In the legacy **V0** engine, chunked prefill was opt-in via `--enable-chunked-prefill`, with `max_num_batched_tokens` — the token budget — defaulting to **2048**. Smaller values "achieve better ITL because there are fewer prefills interrupting decodes"; the documentation recommends `max_num_batched_tokens > 2048` when optimising for throughput.

In the **V1** engine, chunked prefill is **enabled by default**. V1 replaced the two-phase scheduler with a unified one that "treats both prompt and output tokens the same way by using a simple dictionary (e.g., `{request_id: num_tokens}`) to dynamically allocate a fixed token budget per request... without a strict separation between prefill and decode phases." Prefill chunking, prefix caching and speculative decoding all follow from that single abstraction. The V1 default budget is larger — **8192 for online serving, 16384 for offline**.

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

**Tuning `max_num_batched_tokens`.** The parameter is the ITL-versus-throughput knob. Interactive chat, where smooth streaming dominates, favours values at or below the V0 default of **2048**; offline or batch work, where only tokens per second matters, favours values at or above the V1 offline default of **16384**. No published benchmark fixes an optimal value: the setting depends on model, hardware and prompt-length distribution, so it is tuned by measurement. The direction of the effect is what the mechanism fixes — periodic ITL spikes that coincide with the arrival of long prompts indicate the budget is too large, not too small, because the spike *is* the admitted prefill chunk.

The two patterns differ in the axis they cut along: disaggregation *separates* prefill and decode in space (distinct GPUs), chunked prefill *interleaves* them in time (one GPU, one step, one budget). Chunked prefill requires no interconnect and no second pool, only a chosen budget.

## Pitfalls

- **Raising `max_num_batched_tokens` to fix ITL spikes makes them worse.** The spike is the prefill chunk lengthening the iteration that the decodes share; a larger budget admits a larger chunk.
- **Treating chunked prefill as compute-neutral.** Every chunk after the first re-reads the KV cache of its predecessors from HBM, so a heavily sliced prefill performs more total work than one pass.
- **Tuning TTFT and ITL as if they moved together.** They move in opposite directions along the budget axis; a configuration cannot minimise both.
- **Assuming `--enable-chunked-prefill` is needed on V1.** It is enabled by default there, and a configuration copied from a V0 deployment also carries V0's 2048 budget expectations into an engine whose defaults are 8192 online and 16384 offline.
- **Setting the budget below the largest per-step decode demand.** The decodes are admitted first at one token per running sequence, so a budget near `max_num_seqs` leaves no remainder for prefill and prompts make no progress.
- **Reading a low p99 ITL as evidence of good service.** Smooth inter-token latency is compatible with a TTFT that has grown by the number of chunks a long prompt was split into.
