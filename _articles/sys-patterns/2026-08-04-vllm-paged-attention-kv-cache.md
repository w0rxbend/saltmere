---
title: "PagedAttention: paging the KV cache like OS virtual memory"
date: 2026-08-04
track: sys-patterns
summary: "Contiguous KV-cache allocation loses 60-80% of GPU memory to fragmentation and over-reservation. PagedAttention applies operating-system paging: fixed-size blocks, a per-sequence block table, on-demand allocation, and copy-on-write sharing."
reading_time: 8
tags: [llm-serving, vllm, kv-cache, paged-attention, memory-management, gpu, ai-infrastructure]
sources:
  - title: "Kwon et al., Efficient Memory Management for Large Language Model Serving with PagedAttention (SOSP 2023, arXiv:2309.06180)"
    url: "https://arxiv.org/abs/2309.06180"
  - title: "vLLM: Easy, Fast, and Cheap LLM Serving with PagedAttention (vLLM blog)"
    url: "https://blog.vllm.ai/2023/06/20/vllm.html"
  - title: "Optimization and Tuning — vLLM docs"
    url: "https://docs.vllm.ai/en/latest/configuration/optimization.html"
  - title: "Engine Arguments — vLLM docs"
    url: "https://docs.vllm.ai/en/latest/configuration/engine_args.html"
  - title: "Release v0.26.0 — vllm-project/vllm (GitHub)"
    url: "https://github.com/vllm-project/vllm/releases/tag/v0.26.0"
---

**Gist.** An autoregressive transformer caches the key and value (KV) tensors of every token it has seen, and that cache grows one token at a time to a length that is unknown when the request is admitted; allocating a contiguous maximum-length slab per sequence leaves only 20-40% of the allocated KV memory holding real token state. PagedAttention partitions the cache into fixed-size blocks addressed through a per-sequence block table, so blocks are claimed on demand, are uniform in size, and can be reference-counted and shared. The cost is an indirection: the attention kernel must gather non-contiguous blocks through the table rather than stream one flat buffer, and every sequence carries block-table state the allocator has to maintain.

The KV cache exists so that attention over the prefix is not recomputed at each decoding step. It is the dominant and least predictable consumer of graphics processing unit (GPU) memory during serving: the PagedAttention paper puts a 13-billion-parameter model at roughly 800 KB of KV state per token, so a single 2,048-token sequence occupies about 1.6 GB that appears incrementally and vanishes when the request finishes. The difficulty is not the magnitude. It is that the magnitude is not known in advance.

The natural implementation, and the one early serving stacks adopted, gives each sequence one **contiguous** buffer sized to the maximum permitted output length. That allocation decision is the throughput ceiling PagedAttention removes. The pattern sits at the serving layer alongside continuous batching and disaggregated prefill: it is configured by the engine and is transparent to the model weights.

## Why contiguous allocation fragments the cache

A per-sequence maximum-length slab fails in three separable ways, and the pattern attacks each one.

**Internal fragmentation.** The reservation covers the maximum output — say 2,048 tokens — while the request may stop after 200. The remaining 1,848 slots stay allocated and unusable until the request completes.

**Reservation waste.** Even slots for tokens that *will* be generated hold nothing yet. They are reserved from the first decoding step onward, so they are dead weight for most of the request's lifetime.

**External fragmentation.** Different requests reserve different maximum lengths, so the free memory between slabs is cut into holes too small to hold the next sequence's contiguous buffer. This is the classical external fragmentation that paging was introduced to eliminate for operating-system memory.

Profiling of existing systems by the vLLM authors found that only **20-40%** of allocated KV memory held actual token state; the remaining **60-80%** was lost to fragmentation and over-reservation. Fewer sequences resident on the card means a smaller batch, and because decoding is memory-bandwidth bound and amortises weight reads across the batch, a smaller batch means lower throughput. **The memory defect presents as a throughput defect.**

| KV memory on the GPU | Contiguous slab | PagedAttention |
| --- | --- | --- |
| Useful token state | 20-40% | above 96% (waste under 4%) |
| Internal fragmentation | large (unused reserved tail) | ≤ 1 block per sequence |
| Reservation over-allocation | full max-length reserved up front | none — blocks allocated on demand |
| External fragmentation | holes between slabs | none — fixed-size blocks |
| Cross-sequence sharing | impossible | copy-on-write blocks |

## The mechanism: block table and block pool

PagedAttention transfers the virtual-memory construction wholesale. Each sequence's KV cache is split into **fixed-size blocks**, each holding the keys and values for a fixed number of tokens (vLLM's default `block_size` is 16). Blocks need not be contiguous in GPU memory. A per-sequence **block table** maps logical block numbers to physical block addresses, as a page table maps virtual pages to physical frames. The attention kernel is rewritten to gather non-contiguous blocks through this table instead of scanning one flat buffer.

```
Sequence A logical view:      Block table (A)          Physical KV blocks (GPU)
  tok[0..15]   -> logical 0     0 -> phys 7            phys 3: [ ...tok16..31 ]
  tok[16..31]  -> logical 1     1 -> phys 3            phys 7: [ ...tok0..15  ]
  tok[32..40]  -> logical 2     2 -> phys 9            phys 9: [ tok32..40  __ ]  <- 7 free slots
                                                        phys 5: (free)
Allocation is lazy: logical 2 was created only when token 32 arrived.
Only the *last* block (phys 9) has internal fragmentation — at most block_size-1 slots.
```

Three properties follow. Allocation is **on demand**: a new physical block is claimed only when the current one fills, so a request that stops at 200 tokens holds about 13 blocks rather than a 2,048-token reservation. Fragmentation is **bounded by the partially filled final block of each sequence — at most `block_size - 1` slots**, which is why measured waste falls **under 4%**. Because blocks are uniform, no odd-sized holes exist and external fragmentation disappears. Freed blocks return to a shared pool that any sequence may draw from.

The allocator therefore maintains one invariant: **a physical block is either free in the pool or referenced by at least one block table, and its reference count equals the number of block tables pointing at it.** Every append, fork and completion has to preserve that equality.

## Copy-on-write sharing

Once the cache is a set of reference-countable blocks, sharing follows from the same machinery — the second payoff the paper emphasises. In parallel sampling (one prompt, several completions) and in beam search, every candidate shares an identical prompt KV. Under paging, all candidates' block tables point at the *same* physical prompt blocks, so one copy is stored rather than N.

When a shared block must diverge — a candidate appends its own token into a block others still reference — vLLM performs **copy-on-write**: a fresh block is allocated, the shared contents are copied into it, and only that sequence's block table is repointed, in the manner of `fork()` on a memory page. The paper reports the memory saved by this sharing as **6.1-9.8%** for parallel sampling and **37.6-55.2%** for beam search. Freed memory becomes resident batch capacity, and the paper reports vLLM's throughput advantage over prior systems on these workloads in the same 2-4x range it reports elsewhere.

One layering distinction is worth stating precisely: **prefix caching is a policy built on top of paged KV blocks, not a separate mechanism.** Paging supplies content-addressable, reference-counted, shareable blocks; prefix caching is the decision to hash block contents and reuse them across *different requests* sharing a leading span. This article concerns the paging substrate — the allocation and block-table machinery — that admits such policies.

### Implementation sketch (Scala)

The load-bearing part is the allocator: on-demand block acquisition, reference counting, and the copy-on-write branch taken when a shared block is appended to.

```scala
final case class Block(id: Int, var used: Int)          // used <= blockSize

final class KvAllocator(blockSize: Int, totalBlocks: Int):
  private val free = scala.collection.mutable.Queue.range(0, totalBlocks)
  private val refs = Array.fill(totalBlocks)(0)
  private val tables = scala.collection.mutable.Map.empty[Long, Vector[Block]]

  private def acquire(): Block =
    val id = free.dequeue()                              // throws when the pool is exhausted
    refs(id) = 1
    Block(id, used = 0)

  private def release(id: Int): Unit =
    refs(id) -= 1
    if refs(id) == 0 then free.enqueue(id)

  /** Fork shares every block of `src`; divergence is deferred to the first append. */
  def fork(src: Long, dst: Long): Unit =
    val t = tables(src)
    t.foreach(b => refs(b.id) += 1)
    tables(dst) = t

  def append(seq: Long): Unit =
    val t = tables.getOrElse(seq, Vector.empty)
    t.lastOption match
      case Some(last) if last.used < blockSize && refs(last.id) == 1 =>
        last.used += 1                                   // sole owner: write in place
      case Some(last) if last.used < blockSize =>
        val fresh = acquire()                            // copy-on-write
        copyBlock(from = last.id, to = fresh.id)
        fresh.used = last.used + 1
        release(last.id)
        tables(seq) = t.init :+ fresh
      case _ =>
        tables(seq) = t :+ { val b = acquire(); b.used = 1; b }

  def freeSequence(seq: Long): Unit =
    tables.remove(seq).foreach(_.foreach(b => release(b.id)))

  private def copyBlock(from: Int, to: Int): Unit = ???  // device-side block copy
```

The bounded-waste claim is visible here: only the final element of each `Vector[Block]` may have `used < blockSize`.

## Engine parameters

The pattern surfaces as two engine arguments in vLLM (arguments as documented for release **v0.26.0**). `block_size` is the page granularity. `gpu_memory_utilization` sets the fraction of video memory vLLM claims; after weights are loaded and activation scratch is reserved, the remainder becomes the block pool, so raising it yields more blocks and a larger batch.

```python
from vllm import LLM, SamplingParams

llm = LLM(
    model="meta-llama/Llama-3.1-8B-Instruct",
    block_size=16,               # KV page size, in tokens (the pattern's core knob)
    gpu_memory_utilization=0.90, # bigger block pool -> larger batch -> more throughput
)

# One prompt, four samples: the prompt's KV blocks are shared,
# copy-on-write only on the tokens that diverge.
params = SamplingParams(n=4, temperature=0.8, max_tokens=256)
for out in llm.generate(["Explain paging in one paragraph."], params):
    for c in out.outputs:
        print(c.text)
```

Smaller blocks reduce last-block waste but add block-table entries and further kernel indirections; 16 is the shipped default. `gpu_memory_utilization` is the parameter that governs resident batch size: set low it leaves batch capacity unused, set high it risks an out-of-memory failure during activation peaks. Both are parameters of the same pattern — page size, and how much physical memory backs the pool.

The generalisation extends beyond language models: when a resource grows unpredictably and must be shared among tenants, contiguous maximum-sized slabs give way to paging. PagedAttention aims that construction at the KV cache.

## Pitfalls

- **Raising `gpu_memory_utilization` until the pool is largest** leaves no headroom for activation peaks; the failure appears as an out-of-memory error mid-request, not at startup, because weights and scratch are sized before the pool is carved.
- **Reducing `block_size` to shrink last-block waste** trades bounded internal fragmentation for more block-table entries and more gather indirections in the attention kernel.
- **Treating prefix caching as the mechanism** rather than a policy over paged blocks: enabling it changes reuse behaviour, but the fragmentation bound comes from paging and holds with prefix caching off.
- **Assuming forked sequences are independent immediately.** After a fork the block tables alias the same physical blocks; the divergence cost is paid at the first append into a shared block, as a block-sized copy, not at fork time.
- **Reading the sub-4%-waste figure as a per-sequence guarantee.** The bound is at most `block_size - 1` wasted slots per sequence, so a workload of many very short sequences wastes a larger fraction than one of few long sequences.
- **Assuming the block pool is exhausted only by long outputs.** It is exhausted by resident tokens in aggregate; a large batch of short sequences can drain it, and admission then blocks or a sequence is preempted.
