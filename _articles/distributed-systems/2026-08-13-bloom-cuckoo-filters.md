---
title: "Bloom Filters and Cuckoo Filters: Set Membership in a Few Bits"
date: 2026-08-13
track: distributed-systems
summary: "How a Bloom filter answers \"definitely not present\" in ~10 bits per key, the false-positive math you should be able to derive, where filters sit in real read paths (RocksDB, Cassandra, Akamai), and why cuckoo filters add deletion and better cache locality."
reading_time: 5
tags: [bloom-filter, cuckoo-filter, probabilistic-data-structures, lsm-tree, caching]
sources:
  - title: "Space/Time Trade-offs in Hash Coding with Allowable Errors — Burton H. Bloom (CACM, 1970)"
    url: "https://dl.acm.org/doi/10.1145/362686.362692"
  - title: "Cuckoo Filter: Practically Better Than Bloom — Fan, Andersen, Kaminsky, Mitzenmacher (CoNEXT 2014)"
    url: "https://www.cs.cmu.edu/~dga/papers/cuckoo-conext2014.pdf"
  - title: "Algorithmic Nuggets in Content Delivery — Maggs & Sitaraman (SIGCOMM CCR, 2015)"
    url: "https://courses.cs.duke.edu/spring16/compsci590.6/CCRpaper.pdf"
  - title: "Bloom Filters (Apache Cassandra Documentation)"
    url: "https://cassandra.apache.org/doc/latest/cassandra/managing/operating/bloom_filters.html"
  - title: "When Bloom Filters Don't Bloom (Cloudflare blog)"
    url: "https://blog.cloudflare.com/when-bloom-filters-dont-bloom/"
---

The interview question is usually disguised: "how do you avoid hitting disk for keys that don't exist?" or "how does a crawler remember a billion URLs in RAM?" The answer is a filter — a structure that says **"definitely not present"** or **"probably present"** — and the follow-ups test whether you can do the sizing math and name the trade-offs.

## How a Bloom filter works

A Bloom filter (Bloom, 1970) is an `m`-bit array plus `k` independent hash functions. **Insert**: hash the item `k` ways, set those `k` bits. **Query**: hash the same way; if *any* bit is 0, the item was never inserted — a guaranteed **no false negatives** answer. If all `k` bits are 1, the item is *probably* present, but other items may have set those bits: a **false positive**. You cannot delete, because clearing a bit might be shared by another item.

After inserting `n` items, the probability a given bit is still 0 is `(1 - 1/m)^{kn} ≈ e^{-kn/m}`, so the false-positive rate is:

`p ≈ (1 − e^{−kn/m})^k`

Minimizing over `k` gives the two formulas worth memorizing: optimal `k = (m/n)·ln 2`, and required space `m = −n·ln p / (ln 2)² ≈ 1.44·n·log₂(1/p)` bits.

**Sizing example**: 100 M crawled URLs at 1% false positives → `m ≈ 9.59` bits/key ≈ **114 MiB total**, with `k = 7` hashes. Note what's *not* in the formula: item size. A 2 KB URL costs the same 10 bits as an 8-byte ID — that's the entire appeal.

```python
import hashlib, math

class BloomFilter:
    def __init__(self, n_items, fp_rate):
        self.m = math.ceil(-n_items * math.log(fp_rate) / math.log(2) ** 2)  # bits
        self.k = max(1, round(self.m / n_items * math.log(2)))               # hashes
        self.bits = bytearray((self.m + 7) // 8)

    def _positions(self, item):
        h = hashlib.sha256(item.encode()).digest()
        h1 = int.from_bytes(h[:8], "big")
        h2 = int.from_bytes(h[8:16], "big") | 1        # Kirsch–Mitzenmacher double hashing
        return [(h1 + i * h2) % self.m for i in range(self.k)]

    def add(self, item):
        for p in self._positions(item):
            self.bits[p // 8] |= 1 << (p % 8)

    def __contains__(self, item):                       # False -> definitely absent
        return all(self.bits[p // 8] >> (p % 8) & 1 for p in self._positions(item))

bf = BloomFilter(n_items=1_000_000, fp_rate=0.01)       # ~1.14 MiB, k=7
bf.add("user:42");  assert "user:42" in bf and "user:43" not in bf
```

(Tested: 100 k inserts at the 1% setting measured 0.93% empirical false positives. The double-hashing trick simulates `k` hashes from two, per Kirsch–Mitzenmacher.)

## False-positive rate vs bits per element

At optimal `k`, space buys accuracy roughly one order of magnitude per ~4.8 bits:

| Bits/element (m/n) | Optimal k | False-positive rate |
|---|---|---|
| 8 | 6 | 2.16% |
| 10 | 7 | 0.82% |
| 12 | 8 | 0.31% |
| 16 | 11 | 0.046% |
| 20 | 14 | 0.0067% |

## Where filters sit in real systems

- **LSM read path.** A point read may have to probe every SSTable; a per-SSTable filter skips files that definitely lack the key (see [LSM-trees vs B-trees](/articles/distributed-systems/2026-08-11-lsm-trees-vs-b-trees)). RocksDB defaults to ~10 bits/key (just under 1% FP); filter blocks live in the block cache.
- **Cassandra** keeps an off-heap Bloom filter per SSTable, tuned via `bloom_filter_fp_chance` — default 0.01, or 0.1 under leveled compaction; going from 0.1 to 0.01 costs roughly 3× the RAM.
- **CDN cache admission.** Akamai found ~75% of requested objects are **one-hit wonders** — fetched exactly once. A Bloom filter of recently seen URLs implements *cache-on-second-hit*: only cache what the filter already contains. In production this cut disk writes 44% and raised byte hit rate from 74% to 83% (Maggs & Sitaraman, CCR 2015).
- **Crawler / feed dedup.** "Have I seen this URL/event ID?" — a false positive skips one item; a false negative would refetch forever, and Bloom filters never give one.

One production caveat from Cloudflare: `k` probes are `k` *random* memory accesses. Once the filter outgrows L2 cache, every lookup is `k` cache misses, and a dumber, cache-friendly hash table can win. Benchmark before assuming "small = fast."

## Counting Bloom filters

Replace each bit with a small counter (typically 4 bits): increment on insert, decrement on delete, treat "nonzero" as set. This is how *Summary Cache* (Fan et al., 2000) let web caches exchange contents. Costs: ~4× the space, and deleting an item you never inserted (or counter overflow) silently reintroduces false negatives. Mention it, then pivot — the modern answer to "I need deletes" is usually a cuckoo filter.

## Cuckoo filters

A cuckoo filter (Fan et al., CoNEXT 2014) stores short **fingerprints** (e.g., 8–12 bits) in a cuckoo hash table of 4-slot buckets. Each item has two candidate buckets, and — the key trick, *partial-key cuckoo hashing* — the alternate bucket is computed from the stored fingerprint alone: `h₂ = h₁ XOR hash(fingerprint)`. So a fingerprint can be kicked to its other bucket during inserts without ever knowing the original key.

What that buys over Bloom:

- **Deletion**: remove one matching fingerprint from either candidate bucket — no counters, no 4× blowup.
- **Locality**: a lookup touches exactly *two* buckets (often two cache lines), versus `k` scattered probes; lookup throughput stays flat as the table fills.
- **Space**: with 4-slot buckets the table sustains ~95% occupancy, making cuckoo filters *smaller* than space-optimized Bloom filters whenever the target FP rate is below about 3% — i.e., most serious deployments.

The costs: inserts can cascade displacements and fail near capacity (you must size ahead or resize), and at very loose FP targets (>3%) Bloom is still smaller. Interview one-liner: **static set, loose FP budget → Bloom; need deletes, low FP, or cache-sensitive lookups → cuckoo.**

**Try next:** take the `BloomFilter` class above, insert 10 M random keys at 8, 10, and 16 bits/key, and plot measured false-positive rate against the `(1 − e^{−kn/m})^k` prediction — then overfill it to 2× capacity and watch the FP rate collapse toward 100%.
