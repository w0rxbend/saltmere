---
title: "HyperLogLog: Counting Billions of Uniques in 12 Kilobytes"
date: 2026-08-10
track: distributed-systems
summary: How a probabilistic sketch estimates the number of distinct elements in a stream using leading-zero counts and per-bucket maxima, why the sketches merge losslessly across shards, and how the Redis PFADD/PFCOUNT/PFMERGE commands expose them.
reading_time: 6
tags:
  - hyperloglog
  - probabilistic-data-structures
  - cardinality-estimation
  - redis
  - streaming
sources:
  - title: "HyperLogLog: the analysis of a near-optimal cardinality estimation algorithm (Flajolet, Fusy, Gandouet, Meunier, 2007)"
    url: "https://algo.inria.fr/flajolet/Publications/FlFuGaMe07.pdf"
  - title: "HyperLogLog in Practice: Algorithmic Engineering of a State of the Art Cardinality Estimation Algorithm (Heule, Nunkesser, Hall, Google, 2013)"
    url: "https://static.googleusercontent.com/media/research.google.com/en//pubs/archive/40671.pdf"
  - title: "Redis Documentation — PFCOUNT"
    url: "https://github.com/redis/redis-doc/blob/master/commands/pfcount.md"
  - title: "HyperLogLog in Practice — The Morning Paper (Adrian Colyer)"
    url: "https://blog.acolyer.org/2016/03/17/hyperloglog-in-practice-algorithmic-engineering-of-a-state-of-the-art-cardinality-estimation-algorithm/"
---

**Gist.** An exact count of distinct elements requires remembering every element already seen, so a set of `n` distinct 64-bit values costs `O(n)` memory — gigabytes per counter, per dimension, per time window, at a billion uniques. HyperLogLog (HLL) replaces the set with a fixed-size array of small registers holding the maximum leading-zero count of the hashes routed to each register, and estimates cardinality from those maxima. The cost is that the answer is an estimate with a relative standard error of about **1.04/√m** for `m` registers, and individual elements can no longer be tested for membership or removed.

## The problem: distinct counts do not fit in memory

Total event counts are cheap: one counter, incremented forever. Distinct counts — unique visitors per day, distinct client addresses per endpoint, unique search terms — require knowing whether an element has already been counted, which an exact algorithm can only do by storing the elements themselves.

HLL trades exactness for a fixed footprint. Flajolet, Fusy, Gandouet and Meunier showed in 2007 that cardinalities "well beyond 10⁹" can be estimated "with a typical accuracy of 2% while using a memory of only 1.5 kilobytes". The structure is a *sketch*: a small fixed-size summary fed elements one at a time and queried for an estimate. Two sketches over the same register count merge into the sketch of the union of their inputs, exactly.

## The observable: leading zeros

Hash each element to a bit string that behaves as uniformly random. In such a string, the probability of exactly `k` leading zeros is `2^-(k+1)`. If the longest run of leading zeros observed so far is `k`, roughly `2^k` distinct values have probably been hashed: **a rare bit pattern is evidence of many draws**. This single observable — track the maximum leading-zero count `ρ`, estimate `2^ρ` — is the seed of the algorithm.

Its weakness is variance. One hash with an unusually long zero prefix inflates the estimate by orders of magnitude, and a single register carries essentially no averaging.

## Stochastic averaging

HLL reduces that variance by running many estimators over disjoint substreams. The **first `b` bits of the hash select one of `m = 2^b` registers**; the **remaining bits determine `ρ`, the position of the leftmost 1-bit**. Each register retains the maximum `ρ` routed to it. The stream is thereby partitioned into `m` substreams without a second hash pass — *stochastic averaging*.

The 2007 paper combines the registers with the **harmonic mean**, which damps the outlier registers that would otherwise dominate:

```
Z = 1 / Σⱼ 2^(-M[j])
E = α_m · m² · Z
```

`α_m` is a bias-correction constant, asymptotically `α_m ≈ 1/(2 log 2) ≈ 0.72134`. The headline accuracy result is a relative standard error of about **1.04/√m**: precision improves as the square root of a memory cost that grows linearly. At `m = 16384`, `1.04/√16384 = 0.81%`, the figure Redis documents.

When many registers remain at zero, the harmonic-mean estimator is biased, and the original algorithm substitutes **linear counting** — `m · log(m/V)` for `V` empty registers — in the small-range regime.

### Implementation sketch (Scala)

The sketch below shows the load-bearing operations only: routing, the register update, the harmonic-mean estimator with the small-range correction, and the merge. Hashing quality, serialization and register packing are omitted.

```scala
final class HyperLogLog(val b: Int = 14, private val registers: Array[Byte]):
  private val m: Int = 1 << b
  private val alpha: Double = 0.7213 / (1 + 1.079 / m)   // valid for m >= 128

  def add(value: String): Unit =
    val x = hash64(value)
    val idx = (x >>> (64 - b)).toInt                     // first b bits: register
    val rest = x << b                                    // remaining bits, left-aligned
    // leftmost 1-bit position within the remaining 64 - b bits
    val rho = (if rest == 0 then 64 - b else java.lang.Long.numberOfLeadingZeros(rest)) + 1
    if rho > registers(idx) then registers(idx) = rho.toByte

  def count(): Long =
    val z = registers.foldLeft(0.0)((acc, r) => acc + math.pow(2.0, -r.toDouble))
    val e = alpha * m.toDouble * m / z
    val empty = registers.count(_ == 0)
    if e <= 2.5 * m && empty > 0 then (m * math.log(m.toDouble / empty)).toLong
    else e.toLong

  /** Sketch of the union of the two input streams: per-register maximum. */
  def merged(other: HyperLogLog): HyperLogLog =
    require(other.b == b)
    new HyperLogLog(b, Array.tabulate(m)(j => registers(j) max other.registers(j)))

object HyperLogLog:
  def apply(b: Int = 14): HyperLogLog = new HyperLogLog(b, new Array[Byte](1 << b))
```

`merged` is the entire distributed story: `max`.

## Why the sketches merge

Register `j` holds the maximum `ρ` over all elements routed to bucket `j`. If shard A processed one part of the stream and shard B another, the register value for the union is the larger of A's and B's register `j`, because the maximum over `A ∪ B` equals the maximum of the two per-shard maxima. No rescan, no shared state, no coordination — provided both sketches use **the same `b` and the same hash function**.

The consequences:

- One sketch per shard, per minute, per server, maintained independently.
- Minutes rolled into hours and hours into days by merging.
- Fleet-wide uniques computed by taking maxima over a pile of fixed-size blobs.
- Elements added twice, or a stream replayed, without double counting.

Per-register max is idempotent, commutative and associative, so the register array forms a bounded join-semilattice — the structure a state-based conflict-free replicated data type (CRDT) requires. The reduce step in a MapReduce or Flink pipeline is therefore a byte-wise maximum, and the wire cost is constant in the cardinality.

## Redis: PFADD, PFCOUNT, PFMERGE

Redis ships HLL as a native type under the `PF` prefix. It uses **`m = 16384` dense 6-bit registers — 16384 × 6 bits = 12,288 bytes**, "12k bytes for every HyperLogLog", with a 0.81% standard error and cardinalities up to 2⁶⁴.

```
> PFADD visitors:day1 alice bob carol
(integer) 1
> PFADD visitors:day2 carol dave
(integer) 1
> PFCOUNT visitors:day1
(integer) 3
> PFCOUNT visitors:day1 visitors:day2    # on-the-fly union count -> 4 uniques
(integer) 4
> PFMERGE visitors:week visitors:day1 visitors:day2
OK
> PFCOUNT visitors:week
(integer) 4
```

`PFADD` returns 1 when at least one internal register was altered. `PFMERGE` writes the per-register maximum union into a destination key, the persistent form of the multi-key `PFCOUNT`. The documentation notes two operational facts: **multi-key `PFCOUNT` performs an on-the-fly merge whose cost is on the order of a few milliseconds**, and **`PFCOUNT` is technically a write command**, because it caches the computed estimate inside the value it reads.

## HLL++: the 2013 refinements

Heule, Nunkesser and Hall addressed the algorithm's behaviour at both extremes:

- **64-bit hashes** replace 32-bit, so "the large range correction for cardinalities close to 2³² is no longer needed"; hash collisions near four billion stop biasing the estimate.
- **Empirical bias correction** replaces linear counting in the mid-range, using bias measured at roughly 200 cardinalities with k-nearest-neighbour interpolation (k = 6). This removes the error spike the original algorithm exhibits around the 40,960 threshold at p = 14.
- **A sparse representation** stores index–value pairs at higher precision (p' = 25) while most registers are empty, so a sketch tracking a few hundred elements costs far less than the dense array and is more accurate over that range; it converts to the dense representation once the sparse encoding would be the larger of the two.

The leading-zero-plus-per-register-maximum core is unchanged and the merge property is preserved. Later implementations adopt parts of this line of work — Redis, for instance, uses a 64-bit hash and a sparse encoding that upgrades to the dense one.

## Pitfalls

- **Merging sketches built with different `b` or different hash functions produces a meaningless number rather than an error.** The per-register maximum is only the union's register when both sketches partition the hash space identically; a mismatch silently mixes unrelated substreams.
- **Set difference is not supported.** Subtracting `PFCOUNT` of one sketch from another estimates a difference of two noisy quantities, and for similar cardinalities the error can exceed the result. Registers only ever increase, so deletion is impossible.
- **`PFCOUNT` is not a pure read.** It updates the cached estimate stored in the key, so it can dirty a value and propagate a write where a read was expected.
- **Multi-key `PFCOUNT` in a hot path adds latency proportional to the number of keys**, since the union is recomputed on every call; `PFMERGE` into a rollup key moves that cost off the read path.
- **Small cardinalities are the regime where the estimator is biased**, not the safe one: the plain harmonic-mean formula requires the small-range correction, and sketches that skip it under-report on nearly empty register arrays.
- **The 1.04/√m figure is a standard error, not a bound.** Individual sketches deviate further, so a dashboard comparing two HLL-derived numbers that differ by less than a few standard errors is reading noise.
- **Membership cannot be recovered.** A sketch answers "how many distinct", never "was this element present", so an HLL cannot be repurposed for deduplication.
