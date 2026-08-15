---
title: "Jump Consistent Hash: Shard Assignment Without a Ring or a Table"
date: 2026-08-15
track: distributed-systems
summary: "A hash ring needs a sorted table of virtual nodes and a binary search per lookup. Lamping & Veach's 2014 jump consistent hash needs neither — about five lines of arithmetic, no allocated state, O(ln n) expected work, and even distribution with minimal remapping as the cluster grows. The constraint: only the last bucket can be added or removed."
reading_time: 6
tags: [consistent-hashing, jump-hash, sharding, partitioning, load-balancing]
sources:
  - title: "Lamping & Veach, A Fast, Minimal Memory, Consistent Hash Algorithm (arXiv:1406.2294, 2014)"
    url: "https://arxiv.org/abs/1406.2294"
  - title: "Karger et al., Consistent Hashing and Random Trees (STOC 1997)"
    url: "https://dl.acm.org/doi/10.1145/258533.258660"
  - title: "Damian Gryski — Consistent Hashing: Algorithmic Tradeoffs"
    url: "https://dgryski.medium.com/consistent-hashing-algorithmic-tradeoffs-ef6b8e2fcae8"
  - title: "OpenGenus — Jump Consistent Hash"
    url: "https://iq.opengenus.org/jump-consistent-hash/"
---

**Gist.** Consistent hashing keeps key-to-shard assignments stable as a cluster resizes, but the classic ring construction (Karger et al., STOC 1997) pays for even balance with *virtual nodes* — each physical node is scattered across many ring positions, giving an O(V) sorted table and an O(log V) binary search per lookup. Jump consistent hash, published by John Lamping and Eric Veach in 2014, replaces that table with a short arithmetic loop over a linear congruential generator (LCG) seeded by the key, achieving **no allocated state and O(ln n) expected iterations**. The cost is a narrower contract: buckets are integers `0..n-1`, and **only the highest-numbered bucket may be added or removed**.

## The whole algorithm

Given a 64-bit key and a bucket count, the function returns a bucket in `[0, num_buckets)`:

```c
int32_t JumpConsistentHash(uint64_t key, int32_t num_buckets) {
    int64_t b = -1, j = 0;
    while (j < num_buckets) {
        b = j;
        key = key * 2862933555777941757ULL + 1;              // a fast LCG step
        j = (b + 1) * (double(1LL << 31) / double((key >> 33) + 1));
    }
    return b;
}
```

There is no lookup table, no allocation and no sorted structure — the LCG is reused as a stream of pseudo-random numbers whose seed is the key itself.

## Why no table is needed

The construction rests on a probabilistic question: as the bucket count grows from `n` to `n+1`, what is the chance that a given key *jumps* to the newly added bucket? Consistency requires that exactly `1/(n+1)` of keys move to bucket `n` and the rest stay where they were. The algorithm therefore walks the bucket count upward, holding the current answer in `b`, and at each step consults the key-seeded random stream to decide whether this key would have jumped to the newest bucket. **Because the LCG is seeded by the key, the same key always produces the same sequence of jump decisions**, so a key is mapped to the same bucket for a given `num_buckets` on every host, in every process, with no shared state.

The loop does not test every bucket. It uses the random draw to compute the *next* bucket to which the key jumps, skipping over the run of buckets where the key provably stays. The paper proves that the loop body executes fewer than **ln(n) + 1** times on average, that is, O(ln n) expected work. The `key >> 33` shift takes the high 31 bits of the LCG state as the random draw, and the division by `(draw + 1)` converts a uniform variate into the length of that skip.

Two invariants follow from the shape of the loop. First, `j` is non-decreasing, so **a key only ever jumps forward to a newly added bucket**; it never moves between two buckets that both already existed. Second, the return value depends on nothing but the key and `num_buckets`. Together these give minimal remapping on growth: enlarging the cluster from `n` to `n+1` moves only the `1/(n+1)` fraction of keys that belong on the new bucket, and moves them all to that one bucket.

### Implementation sketch (Scala)

```scala
def jumpConsistentHash(key: Long, numBuckets: Int): Int =
  var k = key
  var b = -1L
  var j = 0L
  while j < numBuckets do
    b = j
    k = k * 2862933555777941757L + 1L
    // the division is evaluated first, as in the reference implementation:
    // (b + 1) * 2^31 would exceed the 53-bit mantissa for large b
    j = ((b + 1) * ((1L << 31).toDouble / ((k >>> 33) + 1).toDouble)).toLong
  b.toInt
```

The load-bearing detail is `>>>` rather than `>>`. Scala's `Long` is signed, and the reference implementation treats the LCG state as an unsigned 64-bit value; an arithmetic shift on a negative state yields a negative draw, which makes the computed `j` negative and turns the loop into an infinite one, since `j < numBuckets` then never fails.

## The limitation

Distribution requires no tuning: Lamping & Veach report even balance across buckets with no virtual-node configuration at all. The constraint is elsewhere. **Buckets are identified by their index, numbered `0..n-1`, and the only membership changes the function admits are appending or truncating the highest index.** There is no expression of "remove bucket 4 of 10" — the contract of the function is to map keys evenly onto `num_buckets` slots, and removing a middle bucket would renumber every bucket above it, remapping their keys. A ring, in contrast, permits an arbitrary node to leave, with its keys absorbed by ring neighbours. Lamping & Veach note this restriction themselves and position the algorithm for data-storage sharding rather than for caching topologies whose members come and go.

| | Hash ring (Karger 1997) | Jump consistent hash |
|---|---|---|
| Memory | O(V) sorted vnode table | O(1) — none |
| Lookup time | O(log V) binary search | O(ln n) expected |
| Distribution | even *with* many vnodes | even with no tuning |
| Grow cluster | ~K/N keys move | minimal, provably ~K/N |
| Remove arbitrary node | yes, neighbour absorbs keys | **no** — last bucket only |
| Weighted / heterogeneous nodes | via vnode counts | requires a wrapping layer |
| Named/identified nodes | any id on the ring | integer index only |

The usual workaround for node removal is an indirection table from bucket index to physical server, or a small membership layer wrapped around the hash. Both reintroduce shared state that jump hash itself avoids, and that state must then be kept coherent across clients — the property the algorithm was built to eliminate. Where topology only ever grows at the tail (sharded storage, partitioned queues with sequential shard ids), jump hash dominates a ring on both memory and configuration; where nodes join and leave arbitrarily, the ring's flexibility is what its memory buys.

The remapping bound is cheap to check empirically. Mapping a large sample of random keys at `num_buckets = 1000` and again at `1001` moves approximately `1/1001` of them, and every key that moves lands on bucket 1000 — a direct consequence of the forward-only invariant above.

## Pitfalls

- **Signed right shift on the LCG state.** Using `>>` instead of an unsigned shift in Java, Scala or Go on a negative 64-bit state produces a negative draw, a negative `j`, and a loop that never terminates.
- **Removing a bucket from the middle.** Decrementing `num_buckets` removes the highest index only; deleting shard 4 of 10 by renumbering shifts every higher shard down by one and remaps all of their keys, not shard 4's alone.
- **Passing a weak key.** The function takes an already-hashed 64-bit key, not a raw identifier. Sequential integers or short strings fed in directly inherit their structure into the first LCG step and can skew the distribution; a 64-bit hash of the identifier is the intended input.
- **Non-integer node identity.** Hostnames, IP addresses and UUIDs are not buckets. A separate index-to-server map is required, and its ordering must be identical on every client, or two clients resolve the same key to different servers.
- **Assuming a stable bucket across differing counts.** The result is a function of the key *and* `num_buckets`; a client that has not yet learned of a resize computes a different bucket than one that has, so reads during a resize can miss.
- **Weighted nodes.** Heterogeneous capacity has no expression in the function's contract, which distributes keys uniformly across indices; unequal shard sizes require an external layer that assigns several indices to a larger node.
