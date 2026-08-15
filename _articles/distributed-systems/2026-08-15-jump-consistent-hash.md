---
title: "Jump Consistent Hash: Shard Assignment in Five Lines, No Ring, No Memory"
date: 2026-08-15
track: distributed-systems
summary: "A hash ring needs a sorted table of virtual nodes and a binary search per lookup. Lamping & Veach's 2014 jump consistent hash needs neither — about five lines of arithmetic, zero memory, O(ln n) expected work, and provably even distribution with minimal remapping when you grow the cluster. The catch: it can only add or remove the *last* bucket."
reading_time: 5
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

The classic consistent hash ring (Karger et al., 1997) solved the resharding problem, but it costs something: to keep the distribution even you scatter each real node across hundreds of *virtual nodes* on a sorted ring, then binary-search that table on every lookup. That's O(log(V)) time and O(V) memory, and the memory is real — thousands of nodes times hundreds of vnodes is a table you allocate, sort, and keep coherent. In 2014, John Lamping and Eric Veach (Google) published a hash that throws the table away entirely: **jump consistent hash**, "a fast, minimal memory, consistent hash algorithm that can be expressed in about 5 lines of code."

## The whole algorithm

Given a 64-bit key and a bucket count, it returns a bucket in `[0, num_buckets)`:

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

Or in Python:

```python
def jump_consistent_hash(key: int, num_buckets: int) -> int:
    b, j = -1, 0
    while j < num_buckets:
        b = j
        key = (key * 2862933555777941757 + 1) & 0xFFFFFFFFFFFFFFFF
        j = int((b + 1) * ((1 << 31) / ((key >> 33) + 1)))
    return b
```

No lookup table, no allocation, no sorted structure — just a linear congruential generator reused as a stream of pseudo-random numbers seeded by the key.

## Why it needs no table

The insight is to ask a probabilistic question: as the number of buckets grows from `n` to `n+1`, what is the chance a given key *jumps* to the new bucket? For consistency, exactly `1/(n+1)` of keys should move to bucket `n`, and the rest should stay put. So the algorithm walks the bucket count upward — `b` is the current answer — and at each step uses the key to deterministically decide whether this key would have jumped to the newest bucket. Because the LCG is seeded by the key, the same key always produces the same sequence of jump decisions, so the same key always lands on the same bucket for a given `num_buckets`.

The clever part is that it doesn't check every bucket. It uses the random draw to compute the *next* bucket the key will jump to, skipping over all the buckets in between where it provably stays. The expected number of jumps is therefore small: the paper proves the loop runs fewer than **ln(n) + 1** times on average, i.e. O(ln n). And because a key only ever jumps *forward* to a newly added bucket, growing the cluster remaps the theoretical minimum — a key moves only if it's one of the `1/(n+1)` that belongs on the new bucket.

## The one real limitation

The distribution is near-perfect — Lamping & Veach show it beats the ring's balance without vnodes — but there is a hard constraint. **Buckets must be numbered `0..n-1`, and you can only add or remove the *last* one.** There is no notion of "remove bucket 4 out of 10"; the function's whole contract is "map keys evenly onto `num_buckets` slots." A hash ring, by contrast, lets an arbitrary node leave and hands its keys to its ring neighbors. This is why the paper says jump hash is "more suitable for data storage applications than for distributed web caching": storage shards get sequential ids and grow at the tail, whereas cache servers fail unpredictably in the middle.

| | Hash ring (Karger 1997) | Jump consistent hash |
|---|---|---|
| Memory | O(V) sorted vnode table | O(1) — none |
| Lookup time | O(log V) binary search | O(ln n) expected |
| Distribution | even *with* many vnodes | even with no tuning |
| Grow cluster | ~K/N keys move | minimal, provably ~K/N |
| Remove arbitrary node | yes, neighbor absorbs keys | **no** — last bucket only |
| Weighted / heterogeneous nodes | via vnode counts | awkward (needs wrapping) |
| Named/identified nodes | any id on the ring | integer index only |

The workaround for node removal is to keep a separate mapping from bucket index to physical server, or to combine jump hash with a small membership layer — but that reintroduces some of the state jump hash was designed to avoid. If your topology only ever grows at the end (sharded storage, partitioned queues), jump hash is strictly better than a ring; if nodes come and go arbitrarily, the ring's flexibility is worth its memory.

**Try next:** hash 1M random keys across `num_buckets = 1000`, then bump it to `1001`, and count how many keys changed bucket — you should see almost exactly `1/1001` of them move, and every one that moved should land on bucket 1000.
