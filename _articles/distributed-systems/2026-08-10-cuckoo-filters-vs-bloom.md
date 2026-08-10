---
title: "Bloom vs Cuckoo Filters: The Membership Guards Behind Your Cache"
date: 2026-08-10
track: distributed-systems
summary: Approximate set-membership structures answer "have I seen this key?" in a few bits per element so you can skip a cache miss, a disk seek, or an SSTable read. This walks the Bloom filter (k hash functions, no false negatives, the exact sizing formulas for m and k, why it can't delete) then the cuckoo filter (fingerprints in a cuckoo table with partial-key hashing, deletable, better space below ~3% FP, two-bucket locality) — with a real Bloom implementation, a comparison table, and the load-factor caveat that bites cuckoo.
reading_time: 6
tags:
  - bloom-filter
  - cuckoo-filter
  - probabilistic-data-structures
  - caching
  - lsm-tree
sources:
  - title: "Cuckoo Filter: Practically Better Than Bloom (Fan, Andersen, Kaminsky, Mitzenmacher, CoNEXT 2014)"
    url: "https://www.cs.cmu.edu/~dga/papers/cuckoo-conext2014.pdf"
  - title: "Bloom filter — Wikipedia (optimal m and k formulas, counting variant)"
    url: "https://en.wikipedia.org/wiki/Bloom_filter"
  - title: "RocksDB Bloom Filter — facebook/rocksdb Wiki"
    url: "https://github.com/facebook/rocksdb/wiki/RocksDB-Bloom-Filter"
  - title: "How to Choose Between Bloom Filter and Cuckoo Filter in Redis"
    url: "https://oneuptime.com/blog/post/2026-03-31-redis-how-to-choose-between-bloom-filter-and-cuckoo-filter-in-redi/view"
---

Every read-through cache has a failure mode where the answer is *nothing*. A request arrives for a key that does not exist — not in the cache, not in the database. The cache can't hold "absence" for free, so the request falls through to the store, finds nothing, and returns. Do that a million times a second with random non-existent keys and you have **cache penetration**: your cache is a no-op and your database eats every query. (See the [penetration / breakdown / avalanche writeup](/articles/distributed-systems/2026-08-10-cache-penetration-breakdown-avalanche) for the full taxonomy.)

The guard is a structure that can answer "is this key *possibly* in the set?" using a handful of bits per element — small enough to keep resident in memory in front of a large, slow backing store. Two structures dominate: the **Bloom filter** and the **cuckoo filter**. Both are *approximate*: they trade a tunable false-positive rate for enormous space savings. Neither ever produces a false negative, which is exactly the property you need — "definitely not present" lets you skip the lookup safely.

## The Bloom filter

A Bloom filter is an `m`-bit array, all zero to start, plus `k` independent hash functions. To **insert** element `x`, compute `k` hashes, map each into `[0, m)`, and set those bits to 1. To **test** membership, hash `x` the same way and check whether *all* `k` bits are set. If any is 0, `x` was definitely never inserted — no false negatives. If all `k` are 1, `x` is *probably* present, but the bits could have been set by other elements: a false positive.

The false-positive probability after inserting `n` elements is:

```
p ≈ (1 - e^(-kn/m))^k
```

You don't tune `m` and `k` by hand. Given a target `n` and desired false-positive rate `p`, the optimal bit-array size and hash count are:

```
m = -(n · ln p) / (ln 2)²
k = (m/n) · ln 2
```

These are the classic results (see the Wikipedia derivation). Two consequences worth memorizing for interviews:

- **Space is ~1.44·log₂(1/p) bits per element**, independent of the element size. For `p = 1%`, that's about **9.6 bits per element** with `k ≈ 7`. A billion keys at 1% costs ~1.2 GB — you store keys of any length for roughly a byte each.
- **`k` grows only logarithmically** with tighter `p`. Halving the false-positive rate adds one hash function and ~1.44 bits per element. There is no cliff.

The fatal limitation: **you cannot delete from a standard Bloom filter.** Clearing the `k` bits for `x` might clear a bit that some other element also relies on, creating a false negative — and false negatives break the whole guarantee. The standard fix is the **counting Bloom filter**, which replaces each bit with a small multi-bit counter: insert increments the `k` counters, delete decrements them, and test checks for non-zero. That buys deletion at roughly 3–4× the space (a 4-bit counter per slot instead of 1 bit).

### A real Bloom filter in ~25 lines

```python
import math, mmh3            # mmh3 = MurmurHash3
from bitarray import bitarray

class BloomFilter:
    def __init__(self, n, p):
        # optimal sizing from n (expected items) and p (target FP rate)
        self.m = math.ceil(-(n * math.log(p)) / (math.log(2) ** 2))
        self.k = max(1, round((self.m / n) * math.log(2)))
        self.bits = bitarray(self.m)
        self.bits.setall(0)

    def _slots(self, key):
        # double hashing: k indices from two base hashes (Kirsch–Mitzenmacher)
        h1, h2 = mmh3.hash(key, 0), mmh3.hash(key, 1)
        for i in range(self.k):
            yield (h1 + i * h2) % self.m

    def add(self, key):
        for s in self._slots(key):
            self.bits[s] = 1

    def __contains__(self, key):
        return all(self.bits[s] for s in self._slots(key))   # False = definitely absent
```

The `_slots` trick — deriving `k` indices from just two hashes via `h1 + i·h2` — is the Kirsch–Mitzenmacher optimization; it avoids computing seven independent hashes without measurably worsening `p`. `key not in filter` is your penetration guard: build the filter from every key that exists in the store, and any miss short-circuits before touching the database.

## The cuckoo filter

Fan, Andersen, Kaminsky, and Mitzenmacher introduced the cuckoo filter at CoNEXT 2014 with the subtitle "Practically Better Than Bloom." Instead of setting bits, it stores a short **fingerprint** (a few bits derived from the element's hash) inside a **cuckoo hash table** — an array of buckets, each holding a small number of fingerprint slots (typically `b = 4`).

The clever part is **partial-key cuckoo hashing**, which gives each element two candidate buckets:

```
h1(x) = hash(x)
h2(x) = h1(x) XOR hash(fingerprint(x))
```

Because XOR is its own inverse, you can recover either bucket index from the other *using only the fingerprint stored in the table* — you never need the original element `x`. That's what makes relocation possible: when both candidate buckets are full, the filter evicts (in cuckoo-hashing style) an existing fingerprint and reinserts it into *its* alternate bucket, computed on the fly from the stored fingerprint alone.

This unlocks two things Bloom can't do cleanly:

- **Deletion.** To delete `x`, compute its fingerprint and two buckets and remove one matching fingerprint. Unlike a plain Bloom filter, this is safe — you're removing a specific stored value, not clearing shared bits. (It does assume `x` was actually inserted; deleting a never-inserted item can corrupt the set.)
- **Lookup locality.** A test reads *at most two buckets* — "at most two cache line misses" per the paper — regardless of load or false-positive rate. A Bloom filter with `k = 7` scatters seven reads across the bit array, often seven cache misses.

The fingerprint size sets the false-positive rate: roughly `f ≥ ⌈log₂(1/ε) + log₂(2b)⌉` bits. The paper's headline result: **for ε < 3%, a cuckoo filter uses less space than a space-optimized Bloom filter — while also supporting deletion.** Above ~3%, Bloom is smaller.

### The load-factor caveat

Cuckoo filters are not free lunch. They fill to a hard occupancy limit — the paper reports **95% for `b = 4`, 98% for `b = 8`** — and beyond that, insertions fail: an item bounces between buckets until it hits a maximum eviction count, and the insert is rejected. A Bloom filter never *fails* an insert; it just degrades `p` smoothly as you overfill. So a cuckoo filter needs its capacity provisioned up front with headroom, and a production system must handle the "filter full" signal (resize, or reject). This is the operational tax you pay for deletability and locality.

## Choosing between them

| | Bloom filter | Cuckoo filter |
|---|---|---|
| Delete | No (needs counting variant, ~4×) | Yes, natively |
| Space at ε < 3% | Larger | Smaller |
| Space at ε > 3% | Smaller | Larger |
| Lookups | `k` scattered bit reads (~7 cache misses) | ≤ 2 bucket reads (≤ 2 cache misses) |
| Insert failure | Never fails, `p` degrades | Fails near 95–98% load |
| False negatives | Never | Never (if deletes are well-formed) |

**Pick Bloom** when the set is append-only or rebuilt wholesale (SSTable filters, static allow-lists), when you want dead-simple code, or when your target `p` is loose. **Pick cuckoo** when you need deletion, when you're chasing tight false-positive rates below ~3%, or when lookup latency is dominated by cache misses and bounding them to two matters.

## Where these actually run

- **LSM-tree read filters.** [RocksDB](https://github.com/facebook/rocksdb/wiki/RocksDB-Bloom-Filter) attaches a Bloom filter to each SST file so a point lookup can decide a key "*may exist* or *definitely does not exist*" without reading the file from disk — the single biggest win for negative and sparse lookups. Cassandra does the same per SSTable.
- **Redis.** The RedisBloom module ships both: `BF.RESERVE`/`BF.ADD`/`BF.EXISTS` for scalable Bloom filters, and `CF.RESERVE`/`CF.ADD`/`CF.DEL` for cuckoo filters — the [Redis guidance](https://oneuptime.com/blog/post/2026-03-31-redis-how-to-choose-between-bloom-filter-and-cuckoo-filter-in-redi/view) is to reach for cuckoo specifically when you need deletes.
- **CDN and cache existence checks.** Edge nodes keep a filter of "keys that exist in origin" so a request for a bogus key is rejected at the edge instead of stampeding the origin — the penetration guard, deployed.

These sit alongside two cousins that solve *different* problems, so don't confuse them in an interview: [HyperLogLog](/articles/distributed-systems/2026-08-10-hyperloglog-cardinality-estimation) estimates *how many distinct* elements (cardinality), and Count-Min sketches estimate *how often* an element appears (frequency). Bloom and cuckoo answer only *membership*.

**Try next:** build the Bloom class above, size it for `n = 1e6`, `p = 0.01`, insert a million keys, then measure the empirical false-positive rate against a million never-inserted keys — you should land near 1% and see `m/n ≈ 9.6` bits and `k = 7`. Then sweep `p` from 0.1 down to 0.0001 and watch bits-per-element grow linearly in `log₂(1/p)` while `k` creeps up one hash at a time.
