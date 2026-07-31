---
title: "IBLTs: Reconciling Sets in Space Proportional to the Difference"
date: 2026-07-31
track: distributed-systems
summary: "How Invertible Bloom Lookup Tables let two nodes recover exactly which keys they differ on using space that scales with the size of the diff, not the set — and why that beats Merkle-tree anti-entropy for small deltas."
reading_time: 5
tags: [set-reconciliation, bloom-filter, anti-entropy, distributed-systems, replication, bitcoin]
sources:
  - title: "Invertible Bloom Lookup Tables (Goodrich & Mitzenmacher, 2011)"
    url: "https://arxiv.org/abs/1101.2245"
  - title: "What's the Difference? Efficient Set Reconciliation without Prior Context (Eppstein, Goodrich, Uyeda, Varghese, SIGCOMM 2011)"
    url: "https://research.google/pubs/whats-the-difference-efficient-set-reconciliation-without-prior-context/"
  - title: "Graphene: efficient interactive set reconciliation applied to blockchain propagation (SIGCOMM 2019)"
    url: "https://dl.acm.org/doi/10.1145/3341302.3342082"
  - title: "minisketch: an optimized library for BCH-based set reconciliation (Bitcoin Core)"
    url: "https://github.com/bitcoin-core/minisketch"
  - title: "Extending the XOR Trick to Billions of Rows — Invertible Bloom Filters"
    url: "https://nochlin.com/blog/extending-that-xor-trick"
---

Two replicas hold nearly identical sets of keys. They differ on a handful. The classic fix is Merkle-tree anti-entropy: hash the set into a tree, exchange root hashes, and recurse down mismatched subtrees. That costs O(log n) round trips and, worse, its bandwidth scales with the *set* — you walk a tree of size n even to find one changed key. An **Invertible Bloom Lookup Table (IBLT)** flips the cost model: reconciliation takes space and bandwidth proportional to the *number of differences* d, with prior context of neither side required, in essentially one round.

## The cell

An IBLT is a flat array of m cells. Each cell holds three XOR/sum accumulators — no buckets of keys, just running aggregates:

```python
class Cell:
    def __init__(self):
        self.count   = 0   # signed: how many keys map here
        self.id_sum  = 0   # XOR of all key values
        self.hash_sum = 0  # XOR of hash_c(key) for each key

    def add(self, key, sign=+1):
        self.count   += sign
        self.id_sum  ^= key
        self.hash_sum ^= hash_c(key)
```

Insert maps a key to k cells (say k=4) via k independent hash functions and calls `add(key, +1)` on each. Delete is the exact inverse: `add(key, -1)`. Because id_sum and hash_sum are XOR-folded, a delete perfectly undoes an insert even when other keys share the cell. `hash_c` is a *check* hash, distinct from the placement hashes; it lets decode confirm a recovered key is real rather than XOR noise.

## Peeling

To list the contents, scan for a **pure cell**: one where `count == 1` (or `-1`) and `hash_c(id_sum) == hash_sum`. In a pure cell only one key remains, so `id_sum` *is* that key. Recover it, remove it from its k cells, and that removal may expose new pure cells. Repeat:

```python
def decode(cells):
    added, removed = [], []
    changed = True
    while changed:
        changed = False
        for c in cells:
            if abs(c.count) == 1 and hash_c(c.id_sum) == c.hash_sum:
                key = c.id_sum
                (added if c.count == 1 else removed).append(key)
                for j in placement_cells(key):     # peel out of all k cells
                    cells[j].add(key, sign=-c.count)
                changed = True
    return added, removed          # empty leftover cells => success
```

If every cell ends empty, decode succeeded. If non-empty cells remain with no pure cell, the table was undersized — decode fails cleanly rather than lying.

## Subtracting two tables

The reconciliation trick: build the two IBLTs with the *same* parameters (same m, k, hash functions), then subtract cell-wise. Subtraction is `count_a - count_b`, `id_sum_a ^ id_sum_b`, `hash_sum_a ^ hash_sum_b`. Every key both sides share was XORed in on both — it cancels to zero and vanishes. What survives is exactly the symmetric difference: keys only in A come out with `count == +1`, keys only in B with `count == -1`. Peel the difference table and you get both lists. Crucially, the encoded size depended only on m, and m depends only on d — the shared bulk of the set never touched the wire beyond the fixed table.

## Sizing

The peeling threshold is the same one that governs cuckoo hashing and random hypergraphs: for k≥3 hash functions there is a sharp load factor below which peeling almost surely completes. In practice **m ≈ 1.5·d cells** (the "What's the Difference?" paper's rule of thumb for k=4) decodes with high probability; tighter k=3 works near 1.22·d but is less robust. You need an *estimate* of d to size m — overshoot and you waste space, undershoot and decode fails. Systems either guess conservatively, or run a cheap estimator (a strata estimator, or a coarse min-wise sketch) first, then size the real IBLT.

## Where it ships

Bitcoin's **Graphene** uses IBLTs for block propagation: instead of relaying full transaction IDs, a node sends a small Bloom filter plus an IBLT sized to the few transactions the peer is missing, cutting block-announcement bandwidth by roughly an order of magnitude versus compact blocks for typical mempool overlap. The same shape shows up in database and filesystem replica repair, where the diff is tiny relative to the table.

The known rival is **minisketch** (Bitcoin Core, used in Erlay transaction reconciliation), which encodes differences with BCH codes instead of XOR peeling. Minisketch is *optimal* in size — exactly d entries, no 1.5× overhead — and always decodes given a correct d bound, but decoding costs O(d²) field arithmetic and it only reconciles fixed-width elements. IBLTs trade that ~1.5× space overhead and small failure probability for near-linear decode and the ability to carry arbitrary key/value payloads. Small, frequent diffs of short IDs favor minisketch; larger or richer payloads favor IBLTs.

**Try next:** Implement the `Cell`/`decode` code above in ~60 lines, build two IBLTs over sets that differ by d=20 keys, subtract them, and sweep m from 1.0·d to 2.0·d measuring decode success rate over 1000 trials — you'll watch the peeling threshold appear as a sharp cliff right around 1.2–1.5·d.
