---
title: "PagedAttention: paging the KV cache like OS virtual memory"
date: 2026-08-04
track: sys-patterns
summary: "Contiguous KV-cache allocation wastes 60-80% of GPU memory to fragmentation and over-reservation. PagedAttention borrows the OS paging trick: fixed-size blocks, a per-sequence block table, on-demand allocation, and copy-on-write sharing."
reading_time: 6
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

An autoregressive transformer keeps a running key/value tensor for every token it has seen — the KV cache — so it doesn't recompute attention over the whole prefix on each new token. That cache is the dominant, and least predictable, consumer of GPU memory during serving. A 13B model spends roughly 1MB of KV state per token; a single 2,048-token sequence needs a couple of gigabytes that grow one token at a time and vanish when the request finishes. The hard part isn't the size. It's that you don't know the size in advance.

The natural implementation — the one every early serving stack reached for — is to give each sequence one **contiguous** buffer sized to the maximum possible output length. That single decision is what caps your throughput, and PagedAttention is the pattern that undoes it. It belongs in the same AI-infrastructure toolbox as continuous batching and disaggregated prefill: a mechanism you configure at the serving layer, transparent to the model weights.

## The problem: contiguous allocation fragments the cache

Reserving a max-length contiguous slab per sequence fails in three distinct ways, and it's worth separating them because the pattern attacks each one.

**Internal fragmentation.** You reserve for the maximum output (say 2,048 tokens) but the request stops after 200. The other 1,848 slots sit allocated and unusable until the request completes.

**Reservation waste.** Even the tokens you *will* generate aren't in the cache yet. Their slots are reserved from the first step, so they're dead weight for most of the request's life.

**External fragmentation.** Because different requests reserve different max lengths, the free memory between slabs is chopped into holes too small to hold the next sequence's contiguous buffer — classic external fragmentation, exactly the problem paging was invented to solve for OS memory.

The vLLM authors profiled existing systems and found only **20-40%** of the allocated KV memory held actual token state; the other **60-80%** was lost to fragmentation and over-reservation. Wasted memory means fewer sequences fit on the card, which means a smaller batch, which — because LLM decoding is memory-bandwidth bound and loves large batches — directly means lower throughput. The memory bug is a throughput bug.

| KV memory on the GPU | Contiguous slab | PagedAttention |
| --- | --- | --- |
| Useful token state | 20-40% | ~96%+ |
| Internal fragmentation | large (unused reserved tail) | ≤ 1 block per sequence |
| Reservation over-allocation | full max-length reserved up front | none — blocks allocated on demand |
| External fragmentation | holes between slabs | none — fixed-size blocks |
| Cross-sequence sharing | impossible | copy-on-write blocks |

## The pattern: partition the cache into pages

PagedAttention lifts the OS virtual-memory idea wholesale. Split each sequence's KV cache into **fixed-size blocks**, each holding the keys and values for a fixed number of tokens (vLLM's default `block_size` is 16). Blocks need not be contiguous in GPU memory. A per-sequence **block table** maps logical block numbers to physical block addresses, just as a page table maps virtual pages to physical frames. The attention kernel is rewritten to gather non-contiguous blocks through this table instead of scanning one flat buffer.

```
Sequence A logical view:      Block table (A)          Physical KV blocks (GPU)
  tok[0..15]   -> logical 0     0 -> phys 7            phys 3: [ ...tok16..31 ]
  tok[16..31]  -> logical 1     1 -> phys 3            phys 7: [ ...tok0..15  ]
  tok[32..40]  -> logical 2     2 -> phys 9            phys 9: [ tok32..40  __ ]  <- 7 free slots
                                                        phys 5: (free)
Allocation is lazy: logical 2 was created only when token 32 arrived.
Only the *last* block (phys 9) has internal fragmentation — at most block_size-1 slots.
```

Three properties follow directly. Allocation is **on demand**: a new physical block is claimed only when the current one fills, so a request that stops at 200 tokens holds ~13 blocks, not a 2,048-token reservation. Fragmentation is **bounded**: the only waste is the partially-filled final block of each sequence — at most `block_size - 1` slots — which is why measured waste drops to **under 4%**. And because blocks are uniform, there are no odd-sized holes, so external fragmentation disappears entirely. Freed blocks return to a shared pool that any sequence can draw from.

## Copy-on-write: sharing blocks across sequences

Once the KV cache is a set of reference-countable blocks, sharing is nearly free — the second payoff the paper emphasizes. In parallel sampling (one prompt, several completions) or beam search, every candidate shares the identical prompt KV. With paging, all beams' block tables simply point at the *same* physical prompt blocks; you store one copy, not N.

When a shared block must diverge — a beam appends its own token into a block others still reference — vLLM does **copy-on-write**: it allocates a fresh block, copies the shared prefix into it, and repoints only that sequence's block table, exactly like `fork()` on a memory page. The paper reports this sharing cuts memory for parallel sampling and beam search by up to **55%**, which converts to roughly a **2.2x** throughput gain on those workloads.

This is the layer worth being precise about: **prefix caching is a policy built on top of paged KV blocks, not a separate mechanism.** Paging gives you content-addressable, refcounted, shareable blocks; prefix caching is the decision to hash block contents and reuse them across *different requests* that share a leading span. This article is about the paging substrate — the allocation and block-table machinery — that makes such policies possible.

## The knobs, and a current snippet

The pattern surfaces as two engine arguments in vLLM (current stable **v0.26.0**, July 2026). `block_size` is the page granularity. `gpu_memory_utilization` sets what fraction of VRAM vLLM claims; after loading weights and reserving activation scratch, the rest becomes the block pool, so raising it buys more blocks and a larger batch.

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

You rarely tune `block_size` by hand — smaller blocks shave the last-block waste but add block-table overhead and more kernel indirections; the default of 16 is a good balance. `gpu_memory_utilization` is the lever you actually reach for: too low and you leave batch capacity on the table, too high and you risk out-of-memory during activation peaks. Both knobs are just the pattern's parameters — page size, and how much physical memory backs the pool.

The lesson generalizes past LLMs: when a resource grows unpredictably and must be shared, stop allocating contiguous max-sized slabs and start paging. The OS people settled this in the 1960s; PagedAttention is that same insight aimed at the KV cache, and it's why a single GPU now serves an order of magnitude more traffic than a naive contiguous allocator allowed.

**Try next:** set `gpu_memory_utilization=0.95` and `enable_prefix_caching=True`, serve a workload with a shared system prompt, and watch how many more concurrent sequences fit — then compare to the same run at `0.80`.
