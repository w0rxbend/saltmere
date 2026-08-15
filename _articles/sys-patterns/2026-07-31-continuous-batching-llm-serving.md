---
title: "Continuous Batching: Keeping the GPU Full Between Tokens"
date: 2026-07-31
track: sys-patterns
summary: "Static batching strands GPU cycles when sequences finish early; iteration-level scheduling plus PagedAttention swap requests in and out mid-flight, reported at up to 23x throughput."
reading_time: 6
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

**Gist.** An autoregressive large language model (LLM) emits one token per forward pass, and generation lengths within a batch differ by two orders of magnitude, so a batch held fixed until its slowest member finishes computes over dead rows for most of its life. Continuous batching moves the scheduling boundary from the request to the iteration: membership of the batch is recomputed **before every decode step**, so finished sequences leave immediately and waiting ones take their slots. The cost is a scheduler that runs on the critical path of every token, a key–value (KV) cache allocator that must grant and revoke memory at token granularity, and the possibility of preempting an in-flight sequence when that memory runs out.

## The problem: sequences do not finish together

Batching amortises the cost of streaming model weights out of high-bandwidth memory (HBM) across many concurrent requests. But one prompt may want 12 tokens and another 800.

Under **static batching**, a batch is assembled, run to completion, and only then replaced. Batch latency is therefore dictated by its slowest member. When a short sequence emits its end-of-sequence (EOS) token, its row in the batch stops producing useful output but **stays allocated**: kernels continue to be launched over that slot until the last sequence terminates. On length-skewed workloads a large fraction of the arithmetic performed is over dead rows, and the resource wasted is the most expensive one in the system.

The waste is structural, not a tuning problem. With batch size *B* and per-sequence lengths *L₁ … L_B*, a static batch performs *B · max(Lᵢ)* row-steps to produce *Σ Lᵢ* tokens. Utilisation is *Σ Lᵢ / (B · max Lᵢ)*, which for a heavy-tailed length distribution is small however the batch is sized.

## Iteration-level scheduling

Orca (Yu et al., OSDI 2022) removed the request-level boundary. The scheduler decides membership **before every single decode step**. After each token:

- Any sequence that emitted EOS is evicted and its result returned immediately.
- Waiting requests are admitted into the freed slots.
- The next forward pass runs on the newly composed batch.

This is **continuous** batching, called **in-flight batching** in TensorRT-LLM. A request needing 12 tokens departs after 12 steps rather than after the batch's 800. The batch stays dense and occupancy stays high.

Recomposition is not free, because the members of a batch no longer share a shape. Orca pairs iteration-level scheduling with **selective batching**: the token-parallel matrix multiplications — the feed-forward and projection layers, which treat every token independently — are batched across all sequences, while **attention is computed per sequence**, since each request carries a different KV history and a different length. The invariant that makes the fusion legal is that only attention reads across the token axis of a single sequence; every other operator is pointwise in the sequence dimension.

## Prefill and decode are different workloads

The two phases have different bottlenecks. **Prefill** processes the entire prompt in one pass: many tokens in parallel, compute-bound. **Decode** produces one token per pass: negligible arithmetic per step, bound by the bandwidth required to stream weights and KV state from HBM.

Continuous batching's gain comes almost entirely from packing concurrent **decode** streams, so that a single pass over the weights serves many sequences. This also explains the interference: a long prefill occupies the device for a step during which no decode advances, and every in-flight request observes the stall as inter-token latency. **Chunked prefill** splits a long prompt across several iterations and mixes each chunk with decode work, bounding the stall to the chunk rather than the whole prompt.

## Why PagedAttention makes it practical

Swapping sequences in and out only pays if they fit. The KV cache — the per-token keys and values that every attention step reads — grows with sequence length and dominates memory once the weights are resident. Naive serving reserves a **contiguous slab per request sized for the maximum length**, so a request that stops at 12 tokens holds memory for the length it might have reached. The vLLM paper (SOSP 2023) measured that prior systems used only **20.4%–38.2%** of KV cache memory for actual token state; the remainder was reserved-but-unused, internal fragmentation, and external fragmentation.

**PagedAttention** manages the KV cache the way an operating system manages virtual memory: fixed-size **blocks**, mapped through a per-request **block table**, not required to be contiguous in physical memory. Blocks are allocated on demand as a sequence grows, so **internal fragmentation is bounded by one block per sequence** and external fragmentation disappears. The attention kernel is rewritten to gather through the block table instead of indexing a flat slab.

The recovered memory becomes larger effective batch size, which is the fuel continuous batching burns. The vLLM paper reports **2–4x** higher throughput than prior systems including Orca at comparable latency; the Anyscale benchmark reports up to **23x** over naive static batching, against **8x** for continuous batching without paged memory.

The mechanism introduces a failure mode absent from static batching. Admission decisions are made on memory available now, while a sequence's future length is unknown. When running sequences collectively demand a block that is not available, the scheduler must **preempt**: evict a sequence's blocks, and either recompute its KV state from the prompt later or swap the blocks to host memory. Under sustained overload this degenerates into repeated preempt-and-restore cycles in which throughput falls while the device remains busy.

### Implementation sketch (Scala)

The load-bearing part is the admission and eviction loop, not the kernels.

```scala
final case class Req(id: Long, prompt: Int, emitted: Int, blocks: Int)

final class Scheduler(
    totalBlocks: Int, blockSize: Int, maxSeqs: Int, maxBatchedTokens: Int):

  private var running: Vector[Req] = Vector.empty
  private var waiting: Vector[Req] = Vector.empty
  private def freeBlocks = totalBlocks - running.map(_.blocks).sum

  private def blocksFor(r: Req): Int =
    math.ceil((r.prompt + r.emitted).toDouble / blockSize).toInt

  /** One iteration: grow the survivors, preempt if short, then refill. */
  def step(forward: Vector[Req] => Map[Long, Boolean]): Vector[Req] =
    val finished = forward(running)
    running = running.filterNot(r => finished.getOrElse(r.id, false))

    running = running.map(r => r.copy(emitted = r.emitted + 1))
    while running.map(blocksFor).sum > totalBlocks do
      // Most-recently-admitted first: the victim returns to the queue with its
      // emitted count reset, so its KV state is recomputed rather than stored.
      val victim = running.last
      running = running.init
      waiting = victim.copy(emitted = 0, blocks = 0) +: waiting
    running = running.map(r => r.copy(blocks = blocksFor(r)))

    var budget = maxBatchedTokens - running.size          // 1 decode token each
    while waiting.nonEmpty && running.size < maxSeqs
      && blocksFor(waiting.head) <= freeBlocks
      && waiting.head.prompt <= budget do
      val next = waiting.head
      waiting = waiting.tail
      budget -= next.prompt                               // prefill cost
      running = running :+ next.copy(blocks = blocksFor(next))
    running
```

`maxBatchedTokens` is what couples prefill and decode: a prompt is admitted only if its tokens fit in the same step budget as the decode tokens already committed.

## Operating it

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

- `--gpu-memory-utilization` (default 0.9): fraction of video memory claimed for weights plus KV blocks. A higher value yields more blocks and larger batches, until allocation fails.
- `--max-num-seqs`: ceiling on concurrent sequences per iteration. The scheduler fills up to this many when memory allows.
- `--max-num-batched-tokens`: total tokens, prefill and decode combined, admitted per step. It governs prefill chunk size and the compute-versus-latency trade-off.

TensorRT-LLM and HuggingFace Text Generation Inference implement the same iteration-level pattern.

## Pitfalls

- **Raising `--gpu-memory-utilization` to reduce preemption can cause allocation failure at peak instead.** The fraction covers weights and KV blocks together; activation and workspace memory outside that budget is what the remaining margin absorbs.
- **A large `--max-num-batched-tokens` raises throughput while inter-token latency becomes bursty.** A single step may admit a long prefill, and every decoding sequence waits out that step.
- **Under sustained overload, throughput falls while the device stays at full utilisation.** Preempted sequences are recomputed from their prompts, so the same tokens are prefilled more than once and the arithmetic does not appear as idle time.
- **Measuring throughput at a fixed request count hides the scheduler entirely.** With all requests present at t=0 the queue never drains, so admission never interleaves with decode; the effect of iteration-level scheduling only appears under a request *rate*.
- **Short block sizes bound fragmentation but lengthen the block table.** Internal waste is at most one block per sequence, so the waste is traded against per-step indirection in the attention kernel.
- **A batch whose members all request long outputs behaves like a static batch.** Continuous batching removes waste caused by length *skew*; when lengths are uniform there is no skew to remove.
