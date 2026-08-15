---
title: "Prefix Caching: the KV-reuse pattern for LLM serving"
date: 2026-07-31
track: sys-patterns
summary: "The shared front of every prompt is a cacheable resource. Prefix caching reuses key/value state across requests, and like continuous batching it is a serving pattern that is configured rather than a model that is retrained."
reading_time: 6
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

**Gist.** Every request to a chat model carries the same freight up front — a system prompt, a tool schema, a few exemplars — and the transformer recomputes the attention key/value (KV) tensors for all of it before emitting a single new token. Prefix caching computes the KV state for a token span once, keys it by the token content, and lets any later request whose prompt begins with those same tokens point at the stored blocks instead of recomputing them. The cost is GPU memory held by cached blocks that may never be reused, an eviction policy that must decide what to drop, and a hard dependency on prompt layout: any per-request text placed early invalidates everything after it.

## The mechanism

The reuse is possible because the KV cache is already **paged**. PagedAttention, as implemented in vLLM, stores the cache in fixed-size blocks of tokens rather than in one contiguous buffer per sequence, so a sequence is a list of block handles rather than a slab of memory. Once state is block-structured, sharing is a matter of pointing two sequences at the same block.

The identity of a block is what makes the scheme correct. In vLLM's design, a block's hash is computed from **the hash of its parent block together with the token identifiers it contains**, so the key of the *n*-th block transitively encodes the entire token prefix that precedes it. Two sequences therefore share a block only when they agree on every token up to and including that block — the invariant the attention computation requires, since a KV entry is a function of all tokens to its left. A hash table maps block hash to a resident block; a request's leading blocks are hashed in order and looked up until the first miss, and everything from the miss onward is prefilled normally.

Sharing is consequently **quantized to block boundaries**. Two prompts that agree on 47 tokens with a block size of 16 share two blocks, not 47 tokens: the partially filled third block is not a cache unit, so the last 15 common tokens are recomputed. A full block is also the unit of caching in time — a block is eligible for reuse only once it is full and its hash is fixed.

## Hit rate is a property of the prompt

The measurable lever is the **cache hit rate**, the fraction of prefill tokens served from cache, and it is determined by how much prefix the traffic shares:

- A single system prompt shared by all traffic is a long static prefix that hits on nearly every request.
- Few-shot exemplars pinned ahead of the user turn are shared across an entire workload.
- Multi-turn chat: turn *N* reuses the KV state of turns 1..*N*−1 of the same conversation.
- Retrieval-augmented generation (RAG) in which many queries share a boilerplate instruction header but differ in retrieved chunks hits on the header and misses on the documents.

The corollary is a design constraint on prompt construction: **the variable part goes last**. Because a block hash chains through its parent, a per-request timestamp or user identifier spliced into the top of the system prompt changes the first block's hash and therefore every subsequent block hash, driving the hit rate to zero for the whole prompt rather than for the timestamp alone. Ordering prompt segments from most-shared to least-shared is what preserves the chain.

## Configuration in vLLM

In the V1 engine — made the default in vLLM 0.8.1 — automatic prefix caching is enabled by default, so the operational act is disabling it rather than opting in:

```bash
# APC is already enabled on V1; this is the explicit form
vllm serve meta-llama/Llama-3.1-8B-Instruct \
    --enable-prefix-caching

# to turn it off
vllm serve meta-llama/Llama-3.1-8B-Instruct \
    --no-enable-prefix-caching
```

Two settings govern behaviour. `--block-size` (default 16 tokens on CUDA) fixes the granularity of a cacheable unit, and thus the size of the rounding loss described above. `--prefix-caching-hash-algo` selects how blocks are keyed: `builtin`, Python's `hash`, is the default; `sha256` is offered as the collision-resistant alternative at additional CPU cost. The consequence of a collision is that two distinct token spans map to the same key, so a request can be served KV state computed for tokens it does not contain.

Eviction is **least-recently-used (LRU)** over the pool of free blocks. A cached block is a candidate only once no running sequence still references it; among those, the least recently used are taken first, so hot shared prefixes stay resident and cold one-off conversations age out.

## RadixAttention: reuse over a tree

SGLang's **RadixAttention** (Zheng et al., *SGLang*, arXiv:2312.07104; LMSYS blog, 17 January 2024) generalizes reuse from leading blocks to any shared prefix in a tree. It maintains a **radix tree** whose edges are token sequences and whose nodes reference KV cache tensors. A new request walks the tree from the root, matches the longest shared prefix — including one that branches off a conversation several turns deep — and reuses the matched path, with LRU eviction applied at the leaves.

The additional component is **cache-aware scheduling**: the batch is reordered so that requests sharing a prefix run together, which raises the chance that the shared node is still resident when the later request arrives. The LMSYS blog post reports up to 5x higher throughput than prior systems on its benchmark set; the paper reports up to 6.4x. Both are best-case figures on workloads chosen to share prefixes, and neither bounds what an arbitrary workload gains. Workloads whose branches share a long common history — agent loops, tree-structured search — gain more from the tree formulation than from flat leading-prefix matching.

### Implementation sketch (Scala)

The load-bearing idea is the chained block hash and the prefix walk it enables; the model, the tensors and the allocator are elided.

```scala
opaque type BlockHash = Long

final case class Block(hash: BlockHash, kvSlot: Int)

/** A block's key depends on its parent, so it identifies the whole prefix. */
def blockHash(parent: Option[BlockHash], tokens: Seq[Int]): BlockHash =
  tokens.foldLeft(parent.getOrElse(0L))((h, t) => h * 1000003L + t)

final class PrefixCache(blockSize: Int):
  private val resident = scala.collection.mutable.Map.empty[BlockHash, Block]
  private val recency  = scala.collection.mutable.LinkedHashSet.empty[BlockHash]

  /** Returns the blocks reusable for `prompt` and the token index where
    * prefill must resume. Stops at the first miss: a chained hash makes
    * every later block unreachable once one differs. */
  def matchPrefix(prompt: Seq[Int]): (Vector[Block], Int) =
    // a partially filled trailing block has no fixed hash, so it is never a unit
    val full = prompt.grouped(blockSize).filter(_.sizeIs == blockSize).toVector

    @annotation.tailrec
    def walk(i: Int, parent: Option[BlockHash], hits: Vector[Block]): (Vector[Block], Int) =
      if i == full.length then (hits, i * blockSize)
      else
        val h = blockHash(parent, full(i))
        resident.get(h) match
          case Some(b) =>
            recency.remove(h); recency.add(h)      // touch for LRU
            walk(i + 1, Some(h), hits :+ b)
          case None => (hits, i * blockSize)       // first miss ends the walk

    walk(0, None, Vector.empty)

  def evictOne(): Option[BlockHash] =
    recency.headOption.map { h => recency.remove(h); resident.remove(h); h }
```

## Where the pattern sits

Prefix caching composes with the other serving patterns without interacting with them. Continuous batching keeps the accelerator occupied across requests; prefix caching removes redundant prefill *within* requests; speculative decoding addresses the decode phase. None modifies model weights, and all three can run simultaneously: they are serving-layer configuration, sitting between client and accelerator and transparent to the model.

## Pitfalls

- **A per-request identifier at the top of the system prompt yields a zero hit rate, not a small one.** The block hash chains through its parent, so changing the first block changes every subsequent key.
- **Two prompts that agree on most of a block share none of it.** Sharing is quantized to `--block-size` (16 tokens by default on CUDA); the trailing partial block is always recomputed.
- **`builtin` hashing can alias blocks across tenants.** Python's `hash` is not collision-resistant; `sha256` is the documented alternative, at CPU cost.
- **A high hit rate in a benchmark can vanish under load.** LRU eviction frees the least-recently-used blocks when KV memory fills, so a shared prefix can be evicted between two requests that would otherwise have shared it — which is the gap cache-aware scheduling in SGLang targets.
- **RAG headers hit while retrieved chunks miss.** Documents that vary per query sit after the shared header and are prefilled every time; the hit rate is bounded by the header's share of prompt tokens.
- **Disabling is the explicit action on vLLM V1.** Automatic prefix caching is on by default, so a configuration carried over from an older setup may be passing `--no-enable-prefix-caching` and silently discarding the reuse.
