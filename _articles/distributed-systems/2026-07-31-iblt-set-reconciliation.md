---
title: "IBLTs: Reconciling Sets in Space Proportional to the Difference"
date: 2026-07-31
track: distributed-systems
summary: "How Invertible Bloom Lookup Tables recover exactly which keys two nodes differ on using space that scales with the size of the difference rather than the set, and how that compares with Merkle-tree anti-entropy and BCH-based sketches."
reading_time: 7
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

**Gist.** Two replicas hold nearly identical key sets and must discover the handful of keys on which they disagree. An **Invertible Bloom Lookup Table (IBLT)** encodes each side's set into a fixed array of exclusive-or (XOR) accumulators whose cell-wise subtraction cancels every shared key, leaving only the symmetric difference to be decoded in essentially one round. The cost is that the table must be **sized in advance to an estimate of the difference size d**: too small and decoding fails outright, too large and bandwidth is wasted on empty cells.

## The cost model IBLTs displace

The classic remedy is Merkle-tree anti-entropy: hash the set into a tree, exchange root hashes, and recurse into mismatched subtrees. That costs O(log n) round trips, and its bandwidth is governed by the **size of the set n rather than the size of the difference** — a tree over n elements is walked even when a single key changed. An IBLT inverts this dependence: space and bandwidth scale with d, and neither side requires prior context about what the other holds.

## The cell

An IBLT is a flat array of m cells. A cell stores no keys, only three running aggregates:

- `count` — a **signed** tally of how many keys currently map to the cell;
- `idSum` — the XOR of every key value mapped to the cell;
- `hashSum` — the XOR of `hash_c(key)` over those keys, where `hash_c` is a **check hash distinct from the placement hashes**.

Insertion maps a key to k cells (the "What's the Difference?" analysis uses k = 4) through k independent placement hash functions and applies `+1` to each. Deletion applies the identical update with sign `-1`. Because `idSum` and `hashSum` are XOR-folded and XOR is its own inverse, **a deletion cancels the matching insertion exactly, even when unrelated keys share the cell**. That property is what makes the structure invertible, and it is also what permits `count` to go negative, which the reconciliation step depends on.

The check hash is not redundancy for its own sake. During decoding a cell whose `count` reads 1 may still hold the XOR of several keys whose signs happened to sum to 1; `hash_c` is the test that distinguishes a genuine single key from such **XOR noise**.

## Peeling

Decoding scans for a **pure cell**: one where `|count| == 1` and `hash_c(idSum) == hashSum`. In a pure cell exactly one key remains, so `idSum` *is* that key. The decoder records it, subtracts it from all k of its cells, and that subtraction may render other cells pure. The loop repeats until no pure cell remains.

The termination states are the invariant worth holding onto:

- **All cells empty** — decoding succeeded and the recovered lists are complete.
- **Non-empty cells remain, no cell pure** — the table was undersized. Decoding **fails detectably**; it does not return a partial answer disguised as a complete one. The recovered prefix is still valid, but the caller cannot know which keys are missing.

The residual failure mode that is not detectable is a **check-hash collision**, in which XOR noise satisfies both the count test and the check test and a fabricated key is emitted. The probability of this is governed by the width of `hash_c`.

## Subtracting two tables

Reconciliation requires both sides to build tables with **identical parameters — same m, same k, same hash functions**. The tables are then subtracted cell-wise: `countA - countB`, `idSumA ^ idSumB`, `hashSumA ^ hashSumB`.

Any key present on both sides was folded into the same k cells on both sides, so its contributions cancel and it disappears from the difference table. What survives is exactly the symmetric difference, with the sign of `count` recording provenance: **keys only in A decode with `count == +1`, keys only in B with `count == -1`**. A single peel over the subtracted table therefore yields both the additions and the removals. The bulk of the set never crossed the wire; only the fixed-size table did.

## Sizing

Peeling succeeds or fails according to the same threshold phenomenon that governs random hypergraphs and cuckoo hashing: for k ≥ 3 placement hashes there is a sharp load factor below which peeling almost surely completes and above which it almost surely stalls. The asymptotic thresholds are small constants — lowest at k = 3, around **1.22·d cells**, rising slowly for larger k — but they are limits for large d, so finite tables are sized with margin above them. "What's the Difference?" works with k = 4 and an overhead factor of roughly **1.5**.

This makes an estimate of d a prerequisite rather than an output. Systems either overshoot m conservatively, or run a cheap estimator first — a strata estimator, or a coarse min-wise sketch — and size the real table from its result.

### Implementation sketch (Scala)

```scala
final case class Cell(count: Int, idSum: Long, hashSum: Long):
  def add(key: Long, sign: Int): Cell =
    Cell(count + sign, idSum ^ key, hashSum ^ checkHash(key))
  def isPure: Boolean =
    math.abs(count) == 1 && checkHash(idSum) == hashSum
  def isEmpty: Boolean = count == 0 && idSum == 0L && hashSum == 0L

  // cell-wise subtraction: shared keys cancel because XOR is self-inverse
  def -(o: Cell): Cell = Cell(count - o.count, idSum ^ o.idSum, hashSum ^ o.hashSum)

def placement(key: Long, m: Int, k: Int): Seq[Int] = ???
def checkHash(key: Long): Long = ???

/** Peels a subtracted table. Left = only in A, right = only in B. */
def decode(table: Vector[Cell], m: Int, k: Int): Option[(Set[Long], Set[Long])] =
  @annotation.tailrec
  def loop(cs: Vector[Cell], a: Set[Long], b: Set[Long]): Option[(Set[Long], Set[Long])] =
    cs.indexWhere(_.isPure) match
      case -1 =>
        // no pure cell: either fully decoded, or the table was undersized
        Option.when(cs.forall(_.isEmpty))((a, b))
      case i =>
        val c   = cs(i)
        val key = c.idSum
        val sign = -c.count                       // undo the surviving sign
        val next = placement(key, m, k).foldLeft(cs): (acc, j) =>
          acc.updated(j, acc(j).add(key, sign))
        if c.count == 1 then loop(next, a + key, b) else loop(next, a, b + key)

  loop(table, Set.empty, Set.empty)
```

`placement` and `checkHash` are left unimplemented: the load-bearing content is the cancellation in `-`, the purity test, and the fact that the no-pure-cell branch distinguishes success from undersizing.

## Where it ships

**Graphene** applies IBLTs to Bitcoin block propagation: rather than relaying full transaction identifiers, a node sends a small Bloom filter over the block's transaction identifiers together with an IBLT sized to the residual difference that filter leaves. The receiver reconstructs the block from its own mempool, and the announcement is reported as substantially smaller than a compact block; the published measurements are the authority on how much. The same shape appears in database and filesystem replica repair, where the difference is small relative to the table.

The alternative is **minisketch** (Bitcoin Core, used in Erlay transaction reconciliation), which encodes differences with Bose–Chaudhuri–Hocquenghem (BCH) codes rather than XOR peeling. Minisketch is **size-optimal — exactly d entries, without the ~1.5× peeling overhead — and always decodes given a correct bound on d**, but decoding costs **O(d²) finite-field arithmetic** and it reconciles fixed-width elements only. IBLTs trade the ~1.5× space overhead and a small failure probability for near-linear decoding and the ability to carry arbitrary key/value payloads. Small, frequent differences over short identifiers favour minisketch; larger or richer payloads favour IBLTs.

## Pitfalls

- **Mismatched parameters produce silent garbage.** If the two sides differ in m, k, or hash seeds, shared keys land in different cells and fail to cancel; the subtracted table then encodes most of the union, and decoding either stalls or emits keys that both sides already hold.
- **A failed decode is not a partial decode.** When peeling stalls, the keys recovered so far are correct but the remainder is unknown; treating the partial list as the full difference silently skips repairs.
- **Undersizing d is a cliff, not a gradient.** Below the peeling threshold decoding almost surely succeeds and above it almost surely fails, so a difference slightly larger than the estimate can turn a working reconciliation into a total failure in one round.
- **Reusing the placement hash as the check hash defeats verification.** `hash_c` exists to detect XOR noise in a cell whose signs sum to ±1; if it is derived from the same function that chose the cells, that test loses independence.
- **A narrow check hash emits fabricated keys.** Unlike undersizing, a check-hash collision is not detected: the decoder reports a key neither side holds, and the caller repairs against it.
- **Deletion of a key never inserted corrupts the table.** The XOR accumulators accept the update without complaint and `count` goes negative, so the error surfaces only later as a table that will not peel.
