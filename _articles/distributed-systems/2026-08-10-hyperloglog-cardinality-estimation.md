---
title: "HyperLogLog: Counting Billions of Uniques in 12 Kilobytes"
date: 2026-08-10
track: distributed-systems
summary: How a probabilistic sketch estimates the number of distinct elements in a stream using leading-zero counts and per-bucket maxima, why the sketches merge losslessly across shards, and how to build one in Python or drive Redis PFADD/PFCOUNT/PFMERGE.
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

## The problem: distinct counts don't fit in memory

Counting how many *distinct* things you've seen is deceptively expensive. Total events are cheap — one counter, increment forever. But "how many unique visitors today," "how many distinct IPs hit this endpoint," or "how many unique search terms" all require remembering which items you've already counted. The exact answer needs a set, and a set of `n` distinct 64-bit values costs `O(n)` memory. At a billion uniques that's gigabytes per counter, per dimension, per time window.

HyperLogLog (HLL) trades exactness for a fixed, tiny footprint. Flajolet, Fusy, Gandouet, and Meunier showed in 2007 that you can estimate cardinalities "well beyond 10⁹ with a typical accuracy of 2% while using a memory of only 1.5 kilobytes." The structure is a *sketch*: a small fixed-size summary that you feed elements into and query for an estimate. And critically for distributed systems, two sketches merge into the sketch of their union with no loss.

## The intuition: leading zeros

Hash every element to a uniform random bit string. In a stream of random bits, the probability that a value starts with exactly `k` leading zeros is `2^-(k+1)`. So if the longest run of leading zeros you've *ever* seen is `k`, you've probably observed roughly `2^k` distinct values — seeing a rare pattern is evidence you drew many samples. It's the same logic as: if a friend flipped a coin and got 10 heads in a row at some point, they probably flipped a lot of coins.

That single observable — track the max leading-zero count `ρ`, estimate `2^ρ` — is the seed of the algorithm (Flajolet's earlier work called it the "observable"). The catch is variance: one unlucky hash with 20 leading zeros wrecks the estimate. A single register is basically a coin flip.

## Stochastic averaging: many small buckets

HLL kills the variance by averaging many independent estimators. Use the first `b` bits of each hash to pick one of `m = 2^b` **registers**, and use the *remaining* bits to compute `ρ`, the position of the leftmost 1-bit. Each register keeps the maximum `ρ` it has seen. You've split the stream into `m` substreams, each running its own leading-zero estimator, without a second hash pass — this is *stochastic averaging*.

To combine the registers, the original paper uses the **harmonic mean** (Figure 2), which suppresses the outlier registers that would otherwise inflate the estimate:

```
Z = 1 / Σⱼ 2^(-M[j])
E = α_m · m² · Z
```

`α_m` is a bias-correction constant; asymptotically `α_m ≈ 1/(2 log 2) ≈ 0.72134`. The headline accuracy result is that the relative standard error is about **1.04/√m**. More registers means more precision at a linear memory cost. With `m = 16384` that's `1.04/√16384 = 0.81%` — exactly the number Redis advertises.

## A HyperLogLog from scratch

Here is a complete, runnable HLL in ~30 lines. It uses `b = 14` bits of prefix (16,384 registers), a 64-bit hash, and the low-cardinality "linear counting" correction the original paper applies when many registers are still empty.

```python
import hashlib
from math import log

class HyperLogLog:
    def __init__(self, b=14):
        self.b = b
        self.m = 1 << b                      # number of registers
        self.registers = [0] * self.m
        self.alpha = 0.7213 / (1 + 1.079 / self.m)  # bias constant for m>=128

    def _hash(self, value):
        d = hashlib.sha1(str(value).encode()).digest()
        return int.from_bytes(d[:8], "big")  # 64-bit

    def add(self, value):
        x = self._hash(value)
        idx = x >> (64 - self.b)             # first b bits -> register index
        rest = (x << self.b) & ((1 << 64) - 1)  # remaining bits, left-aligned
        rho = 64 - self.b - rest.bit_length() + 1 if rest else 64 - self.b + 1
        self.registers[idx] = max(self.registers[idx], rho)

    def count(self):
        Z = sum(2.0 ** -r for r in self.registers)
        E = self.alpha * self.m * self.m / Z
        if E <= 2.5 * self.m:                # small-range: linear counting
            V = self.registers.count(0)
            if V:
                return int(self.m * log(self.m / V))
        return int(E)

    def merge(self, other):                  # union = per-register max
        assert self.m == other.m
        self.registers = [max(a, b) for a, b in zip(self.registers, other.registers)]

hll = HyperLogLog()
for i in range(1_000_000):
    hll.add(f"user:{i}")
print(hll.count())   # ~1,000,000 (typically within ~1%)
```

The `merge` method is three characters of logic — `max` — and it's the whole reason HLL matters for distributed systems.

## Why the sketches merge: union = per-register max

Register `j` holds the maximum `ρ` observed among all elements that hashed into bucket `j`. If shard A processed some elements and shard B processed others, the correct register value for the *union* is simply the larger of A's and B's register `j` — because "the max over A∪B" equals "the max of (max over A) and (max over B)." No re-scanning, no shared state, no coordination.

This makes HLL an idempotent, commutative, associative sketch. You can:

- Maintain one HLL per shard, per minute, per server, entirely independently.
- Roll minutes into hours and hours into days by merging.
- Compute "uniques across the whole fleet last week" by max-ing a pile of 12 KB blobs.
- Add the same element twice, or replay a stream, with no double-counting.

That's a CRDT-flavored property: sketches are a bounded join-semilattice under per-register max. It's why MapReduce jobs, Flink pipelines, and analytics warehouses lean on HLL — the reduce step is trivial and the wire cost is constant regardless of cardinality.

## Redis: PFADD, PFCOUNT, PFMERGE

Redis ships HLL as a native type (the `PF` prefix honors Philippe Flajolet). It uses `m = 16384` dense 6-bit registers — 16384 × 6 bits = 12,288 bytes, "12k bytes for every HyperLogLog," with a 0.81% standard error and cardinalities up to 2⁶⁴.

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

`PFADD` returns 1 if the sketch's estimate probably changed. `PFMERGE` writes the per-register max union into a destination key — the persistent form of the multi-key `PFCOUNT`. Note the docs' warning: multi-key `PFCOUNT` does an on-the-fly merge that can take milliseconds, and `PFCOUNT` is technically a write because it caches the estimate in the last 8 bytes of the value.

## HLL++: what Google fixed in 2013

The original algorithm has two rough edges at the extremes. Heule, Nunkesser, and Hall's "HyperLogLog in Practice" addressed them:

- **64-bit hashes** replace 32-bit, so "the large range correction for cardinalities close to 2³² is no longer needed" — collisions near four billion stop biasing the estimate.
- **Empirical bias correction** replaces linear counting in the mid-range, using measured bias at ~200 cardinalities with k-nearest-neighbor interpolation (k=6), removing the error spike the original showed around the 40,960 threshold at p=14.
- **Sparse representation** stores index–value pairs (at a higher precision, p'=25) when most registers are empty, so a sketch tracking a few hundred items costs far less than 12 KB and is up to 4× more accurate for cardinalities below ~12,000, only converting to the dense array when it grows.

These are engineering refinements, not a new idea — the leading-zero-plus-per-register-max core is unchanged, and the merge property is preserved. Redis, Presto, BigQuery, and Druid all ship HLL++-flavored variants.

**Try next:** build the Python sketch above, feed it 1M items, and plot estimate-vs-truth as you sweep `b` from 4 to 16 — watch the error track 1.04/√m and confirm that `a.merge(b)` gives the same count as adding both streams to one sketch.
