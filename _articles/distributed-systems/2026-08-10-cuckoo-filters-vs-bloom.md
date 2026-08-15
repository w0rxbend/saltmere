---
title: 'Bloom vs Cuckoo Filters: Membership Guards in Front of a Cache'
date: 2026-08-10
track: distributed-systems
summary: Approximate set-membership structures answer "has this key been seen?" in a few bits per element, allowing a cache miss, a disk seek or an SSTable read to be skipped. This covers the Bloom filter (k hash functions, no false negatives, the sizing formulas for m and k, the reason deletion is unsound) then the cuckoo filter (fingerprints in a cuckoo table under partial-key hashing, deletable, smaller below ε ≈ 3%, two-bucket locality) — with an implementation sketch, a comparison table, and the load-factor limit that constrains cuckoo.
reading_time: 6
tags:
- bloom-filter
- cuckoo-filter
- probabilistic-data-structures
- caching
- lsm-tree
sources:
- title: 'Cuckoo Filter: Practically Better Than Bloom (Fan, Andersen, Kaminsky, Mitzenmacher, CoNEXT 2014)'
  url: https://www.cs.cmu.edu/~dga/papers/cuckoo-conext2014.pdf
- title: Bloom filter — Wikipedia (optimal m and k formulas, counting variant)
  url: https://en.wikipedia.org/wiki/Bloom_filter
- title: RocksDB Bloom Filter — facebook/rocksdb Wiki
  url: https://github.com/facebook/rocksdb/wiki/RocksDB-Bloom-Filter
- title: How to Choose Between Bloom Filter and Cuckoo Filter in Redis
  url: https://oneuptime.com/blog/post/2026-03-31-redis-how-to-choose-between-bloom-filter-and-cuckoo-filter-in-redi/view
- title: Space/Time Trade-offs in Hash Coding with Allowable Errors — Burton H. Bloom (CACM, 1970)
  url: https://dl.acm.org/doi/10.1145/362686.362692
- title: Algorithmic Nuggets in Content Delivery — Maggs & Sitaraman (SIGCOMM CCR, 2015)
  url: https://courses.cs.duke.edu/spring16/compsci590.6/CCRpaper.pdf
- title: Bloom Filters (Apache Cassandra Documentation)
  url: https://cassandra.apache.org/doc/latest/cassandra/managing/operating/bloom_filters.html
- title: When Bloom Filters Don't Bloom (Cloudflare blog)
  url: https://blog.cloudflare.com/when-bloom-filters-dont-bloom/
---

**Gist.** A read-through cache cannot store absence cheaply, so a stream of requests for keys that do not exist passes through the cache and reaches the store on every request — **cache penetration**. An approximate membership structure placed in front of the store answers "possibly present" or "definitely absent" in a few bits per element, and the "definitely absent" answer terminates the lookup without touching the store. The cost is a tunable false-positive rate: some non-existent keys are still forwarded, and the structure must be kept consistent with the set it guards.

The taxonomy of penetration, breakdown and avalanche is covered in the [companion writeup](/articles/distributed-systems/2026-08-10-cache-penetration-breakdown-avalanche). Two structures dominate the guard role: the **Bloom filter** and the **cuckoo filter**. Both are one-sided: **neither ever produces a false negative**, which is the property the guard depends on — a negative answer is authoritative, and only a positive answer needs verification against the store.

## The Bloom filter

A Bloom filter is an `m`-bit array, initially all zero, plus `k` independent hash functions. **Insert** of element `x` computes `k` hashes, maps each into `[0, m)`, and sets those bits to 1. **Test** hashes `x` identically and checks whether *all* `k` bits are set. If any bit is 0, `x` was never inserted. If all `k` are 1, `x` is probably present — but the bits may have been set by other elements, which is the false positive.

The false-positive probability after inserting `n` elements is:

```
p ≈ (1 - e^(-kn/m))^k
```

Given a target `n` and a desired false-positive rate `p`, the optimal bit-array size and hash count are:

```
m = -(n · ln p) / (ln 2)²
k = (m/n) · ln 2
```

Two consequences follow from these formulas:

- **Space is ≈ 1.44·log₂(1/p) bits per element**, independent of the element's own size. For `p = 1%` this is about **9.6 bits per element** with `k ≈ 7`. One billion keys at 1% therefore costs roughly 1.2 GB, whatever the key length.
- **`k` grows logarithmically** as `p` tightens. Halving the false-positive rate adds one hash function and about 1.44 bits per element. The cost curve has no cliff.

The structural limitation is deletion. **Clearing the `k` bits of `x` is unsound**, because any of those bits may also be the evidence for a different inserted element; clearing it produces a false negative, and a false negative invalidates the guard's only authoritative answer. The standard remedy is the **counting Bloom filter**, in which each bit becomes a small multi-bit counter: insert increments the `k` counters, delete decrements them, and test checks for non-zero. Deletion then costs about 4× the space when a **4-bit counter replaces each 1-bit slot**.

### Implementation sketch (Scala)

The load-bearing detail is slot derivation: `k` indices are obtained from two base hashes via `h1 + i·h2`, the **Kirsch–Mitzenmacher double-hashing construction**, which avoids computing `k` independent hashes without measurably worsening `p`.

```scala
final class BloomFilter(n: Int, p: Double):
  private val m: Int = math.ceil(-(n * math.log(p)) / (math.log(2) * math.log(2))).toInt
  private val k: Int = math.max(1, math.round((m.toDouble / n) * math.log(2)).toInt)
  private val bits = new java.util.BitSet(m)

  // Two base hashes; the i-th slot is h1 + i*h2 (Kirsch-Mitzenmacher).
  private def slots(key: String): Iterator[Int] =
    val h1 = scala.util.hashing.MurmurHash3.stringHash(key, 0x9747b28c)
    val h2 = scala.util.hashing.MurmurHash3.stringHash(key, 0x5bd1e995) | 1
    Iterator.range(0, k).map(i => math.floorMod(h1 + i * h2, m))

  def add(key: String): Unit = slots(key).foreach(bits.set)

  /** false => definitely absent; true => possibly present. */
  def mayContain(key: String): Boolean = slots(key).forall(bits.get)

  def bitsPerElement: Double = m.toDouble / n
```

`mayContain` returning `false` is the penetration guard: the filter is populated from every key present in the store, and a negative answer short-circuits before the store is queried.

## The cuckoo filter

Fan, Andersen, Kaminsky and Mitzenmacher introduced the cuckoo filter at CoNEXT 2014 under the subtitle "Practically Better Than Bloom". Rather than setting bits, it stores a short **fingerprint** — a few bits derived from the element's hash — inside a **cuckoo hash table**: an array of buckets, each holding a small number of fingerprint slots, typically `b = 4`.

The mechanism that makes this work is **partial-key cuckoo hashing**, which assigns each element two candidate buckets:

```
h1(x) = hash(x)
h2(x) = h1(x) XOR hash(fingerprint(x))
```

Because XOR is its own inverse, **either bucket index can be recovered from the other using only the fingerprint stored in the table**; the original element `x` is never needed. That invariant is what permits relocation. When both candidate buckets are full, the filter evicts a resident fingerprint in cuckoo-hashing style and reinserts it into *its* alternate bucket, computed on the fly from the stored fingerprint alone.

Two properties follow that a plain Bloom filter does not offer:

- **Deletion.** Deleting `x` computes its fingerprint and its two buckets and removes one matching fingerprint. This removes a specific stored value rather than clearing shared bits. It assumes `x` was inserted: deleting an item that was never inserted may remove a colliding fingerprint belonging to another element, which reintroduces false negatives.
- **Lookup locality.** A test reads **at most two buckets, so at most two cache lines** — regardless of load factor or false-positive rate. A Bloom filter with `k = 7` scatters seven reads across the bit array, frequently seven cache misses.

Fingerprint width sets the false-positive rate: approximately `f ≥ ⌈log₂(1/ε) + log₂(2b)⌉` bits. The paper's headline result is that **for ε < 3% a cuckoo filter uses less space than a space-optimized Bloom filter while also supporting deletion**; above roughly 3%, Bloom is the smaller structure.

### The load-factor limit

A cuckoo filter fills to a hard occupancy limit — the paper reports **95% for `b = 4` and 98% for `b = 8`**. Beyond it, insertion fails: the displaced item bounces between alternate buckets until a maximum eviction count is reached, and the insert is rejected. A Bloom filter never fails an insert; it degrades `p` continuously as `n` exceeds the provisioned figure. A cuckoo filter therefore requires capacity provisioned in advance with headroom, and the surrounding system must handle the "filter full" signal by resizing or rejecting.

## Choosing between them

| | Bloom filter | Cuckoo filter |
|---|---|---|
| Delete | No (counting variant, ~4× space) | Yes, natively |
| Space at ε < 3% | Larger | Smaller |
| Space at ε > 3% | Smaller | Larger |
| Lookups | `k` scattered bit reads (~7 cache misses) | ≤ 2 bucket reads (≤ 2 cache misses) |
| Insert failure | Never fails, `p` degrades | Fails near 95–98% load |
| False negatives | Never | Never, if deletes are well-formed |

Bloom fits sets that are append-only or rebuilt wholesale (SSTable filters, static allow-lists) and targets where `p` is loose. Cuckoo fits sets requiring deletion, false-positive targets below roughly 3%, or lookup paths where cache misses dominate latency and a bound of two matters.

## Where these run

- **LSM-tree read filters.** [RocksDB](https://github.com/facebook/rocksdb/wiki/RocksDB-Bloom-Filter) attaches a Bloom filter to each SST file, so a point lookup decides "may exist" or "definitely does not exist" without reading the file from disk. Cassandra applies the same construction per SSTable.
- **Redis.** The RedisBloom module ships both families: `BF.RESERVE`/`BF.ADD`/`BF.EXISTS` for scalable Bloom filters and `CF.RESERVE`/`CF.ADD`/`CF.DEL` for cuckoo filters. A [comparison of the two in Redis](https://oneuptime.com/blog/post/2026-03-31-redis-how-to-choose-between-bloom-filter-and-cuckoo-filter-in-redi/view) selects cuckoo specifically where deletes are required.
- **Content delivery network (CDN) existence checks.** Edge nodes hold a filter of keys known to exist at origin, so a request for a key absent from the filter is rejected at the edge instead of reaching origin.

Two related sketches answer different questions and should not be substituted: [HyperLogLog](/articles/distributed-systems/2026-08-10-hyperloglog-cardinality-estimation) estimates how many distinct elements a stream contains (cardinality), and Count-Min sketches estimate how often an element appears (frequency). Bloom and cuckoo filters answer membership only.

The counting Bloom filter deserves a note of its own: replacing each bit with a small counter, typically 4 bits, is the construction that allowed the web caches of *Summary Cache* (Fan et al., 2000) to exchange their contents. Its costs are the ~4× space and the fact that deleting an item never inserted, or overflowing a counter, silently reintroduces false negatives.

## Pitfalls

- **Deleting a key from a plain Bloom filter by clearing its `k` bits** produces false negatives for any other element that shared one of those bits; the guard then reports "definitely absent" for a key that exists and the read is skipped incorrectly.
- **Deleting a never-inserted key from a cuckoo filter** removes a fingerprint belonging to a colliding element, with the same false-negative consequence.
- **Sizing a Bloom filter for fewer elements than the workload inserts** does not fail loudly; `p` rises continuously as `n` grows past the provisioned figure, so the guard silently stops filtering while still reporting success on every insert.
- **Provisioning a cuckoo filter without headroom** produces insert rejections near 95% load for `b = 4`; a caller that ignores the failure return leaves keys absent from the filter, which is again a false negative for those keys.
- **Populating the filter from the store without keeping it in sync** means newly written keys are missing from the filter; because a Bloom filter cannot delete, the usual remedy is a periodic full rebuild rather than incremental repair.
- **Assuming a positive answer means presence.** Every positive is "possibly present" and must still be verified against the store; treating it as authoritative returns data for keys that were never written.
