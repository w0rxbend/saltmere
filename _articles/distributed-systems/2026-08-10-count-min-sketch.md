---
title: 'The Count-Min Sketch: Frequency Estimation in Sublinear Space'
date: 2026-08-10
track: distributed-systems
summary: How a two-dimensional array of counters and d hash functions estimates how often you've seen a key using kilobytes instead of gigabytes, why it overestimates but never underestimates, the ε ≈ e/w error bound, the conservative-update trick, and how it powers TinyLFU cache admission and hot-key detection.
reading_time: 6
tags:
- count-min-sketch
- probabilistic-data-structures
- frequency-estimation
- tinylfu
- caching
- streaming
- heavy-hitters
sources:
- title: 'An Improved Data Stream Summary: The Count-Min Sketch and its Applications (Cormode & Muthukrishnan, 2005) — encyclopedia entry'
  url: http://dimacs.rutgers.edu/~graham/pubs/papers/encalgs-cm.pdf
- title: Count–min sketch — Wikipedia
  url: https://en.wikipedia.org/wiki/Count%E2%80%93min_sketch
- title: 'TinyLFU: A Highly Efficient Cache Admission Policy (Einziger, Friedman, Manes, 2017)'
  url: https://dl.acm.org/doi/10.1145/3149371
- title: W-TinyLFU Eviction Policy — Caffeine documentation
  url: https://www.mintlify.com/ben-manes/caffeine/advanced/efficiency
- title: 'Count-Min Sketch with Conservative Updates: Worst-Case Analysis (arXiv 2405.12034)'
  url: https://arxiv.org/html/2405.12034v1
- title: 'An Improved Data Stream Summary: The Count-Min Sketch and its Applications — Cormode & Muthukrishnan (J. Algorithms, 2005)'
  url: http://dimacs.rutgers.edu/~graham/pubs/papers/cm-full.pdf
- title: Count-Min Sketch — Graham Cormode's project page
  url: https://sites.google.com/site/countminsketch/
- title: 'Count-Min Sketch: The Art and Science of Estimating Stuff — Redis Blog'
  url: https://redis.io/blog/count-min-sketch-the-art-and-science-of-estimating-stuff/
- title: Count-min sketch — Redis Documentation (CMS commands)
  url: https://redis.io/docs/latest/develop/data-types/probabilistic/count-min-sketch/
---

## The problem: counting frequencies you can't afford to store

Plenty of systems need to answer "how many times have I seen this key?" A cache wants to know whether a candidate is accessed often enough to be worth keeping. A load balancer wants to spot the one product ID that's suddenly getting a million requests a second. A network box wants the flows hogging the link. The exact answer is a hash map from key to count — and that map grows with the number of *distinct* keys, which for URLs, IPs, or user IDs can be billions. One `int64` counter per key is gigabytes, and most of those keys you'll see once and never again.

The **Count-Min Sketch** (Cormode & Muthukrishnan, 2005) trades exactness for a fixed, tiny footprint. Like HyperLogLog it's a *sketch* — a small summary you feed items into and query — but it answers a different question. HyperLogLog estimates *cardinality* (how many distinct things); Count-Min estimates *frequency* (how often a specific thing). Same probabilistic spirit, orthogonal use. (See the companion article on [HyperLogLog](/articles/distributed-systems/2026-08-10-hyperloglog-cardinality-estimation).)

## The structure: d rows, w counters, one increment per row

A Count-Min Sketch is a two-dimensional array of counters with **depth `d`** (rows) and **width `w`** (columns), plus `d` independent hash functions `h₁…h_d`, each mapping a key to a column in `{1…w}`. The hashes come from a pairwise-independent family.

Two operations:

- **Add `(key, c)`:** for every row `j`, increment `table[j][h_j(key)] += c`. One counter touched per row — `O(d)` work, independent of `w`.
- **Estimate `(key)`:** return `min` over all rows of `table[j][h_j(key)]`.

That's the whole thing. Each row is an independent, lossy histogram: many keys collide into the same column and their counts pile up together. But because the rows use *different* hash functions, two keys that collide in row 1 almost certainly land in different columns in row 2. Taking the minimum across rows picks the row where your key suffered the least collision noise — hence "Count-*Min*."

## Why it overestimates but never underestimates

Every increment for `key` lands on the counters `key` hashes to, so each of those counters is *at least* the true count of `key`. Collisions from *other* keys can only push those counters higher, never lower. So every row gives an overestimate, and the minimum of overestimates is still ≥ the truth:

```
estimate(key) ≥ true_count(key)     always
```

The sketch is a **one-sided estimator**: it can tell you a key is hotter than it is, but it will never hide a hot key. That asymmetry is exactly what you want for admission control and heavy-hitter detection — you'd rather occasionally admit a lukewarm key than ever reject a genuinely hot one.

## The error bounds (get these right)

The two dimensions are set directly from the accuracy you want. Given a target additive error `ε` and a failure probability `δ`:

```
w = ⌈e / ε⌉          (e = Euler's number ≈ 2.718)
d = ⌈ln(1 / δ)⌉
```

The guarantee, from the paper: the estimate never underestimates, and **with probability at least `1 − δ`,**

```
estimate(key) ≤ true_count(key) + ε · ‖a‖₁
```

where `‖a‖₁` is the total number of items added (the L1 norm of the frequency vector). The error is *additive* and scaled by the *total stream size*, not by the individual key's count. So wider rows (`w`) shrink `ε` — the overcount per query — while more rows (`d`) drive down `δ`, the probability that all rows got unlucky at once. Space is `O(w·d)` counters, i.e. `O((1/ε)·ln(1/δ))` — logarithmic in the confidence, and completely independent of how many distinct keys you throw at it.

Concretely: `ε = 0.001`, `δ ≈ 0.001` gives `w = ⌈e/0.001⌉ = 2719` and `d = ⌈ln 1000⌉ = 7`. That's ~19,000 counters — a handful of kilobytes — to estimate every key's frequency in a billion-item stream to within 0.1% of the stream size, 99.9% of the time.

## An implementation in ~30 lines

```python
import hashlib

class CountMinSketch:
    def __init__(self, w=2719, d=7):     # ε≈0.001, δ≈0.001
        self.w, self.d = w, d
        self.table = [[0] * w for _ in range(d)]

    def _cols(self, key):                 # one column per row
        for j in range(self.d):
            h = hashlib.blake2b(str(key).encode(), digest_size=8,
                                person=j.to_bytes(8, "big")).digest()
            yield int.from_bytes(h, "big") % self.w

    def add(self, key, count=1):
        for j, col in enumerate(self._cols(key)):
            self.table[j][col] += count

    def estimate(self, key):
        return min(self.table[j][col] for j, col in enumerate(self._cols(key)))

    def add_conservative(self, key, count=1):
        cols = list(self._cols(key))
        target = min(self.table[j][c] for j, c in enumerate(cols)) + count
        for j, c in enumerate(cols):      # only raise counters below target
            if self.table[j][c] < target:
                self.table[j][c] = target

cms = CountMinSketch()
for _ in range(5000): cms.add("hot-key")
for i in range(100000): cms.add(f"cold-{i}")
print(cms.estimate("hot-key"))    # ≥ 5000, slightly inflated by collisions
print(cms.estimate("cold-42"))    # ≥ 1, occasionally more
```

The `person` parameter of BLAKE2 gives us `d` distinct hash functions from one primitive — cheaper than seeding `d` separate hashers.

## The conservative-update optimization

Notice `add` blindly bumps every one of a key's counters. But since the query only ever reads the *minimum*, you don't need the larger counters to grow at all. **Conservative update** (Estan & Varghese's "minimal increment") computes the post-increment estimate — `current_min + count` — and then raises each of the key's counters only up to that target, leaving already-larger counters untouched (`add_conservative` above).

This keeps the no-underestimate guarantee while sharply reducing the overcount, because counters shared with other keys stop absorbing increments they don't need — it cuts average error several-fold on skewed workloads. The tradeoff: it breaks the *linear-sketch* property, so two conservatively-updated sketches can no longer be summed counter-by-counter to merge (recent worst-case analysis in the arXiv paper below).

## The caching connection: TinyLFU admission

Here's where the sketch earns its keep in cache design. Pure LRU admits *everything* on access, so a one-hit scan can evict genuinely popular entries. LFU keeps the frequently-used, but a classic LFU needs a real counter per key — the very cost we're avoiding. **TinyLFU** (Einziger, Friedman & Manes, 2017) resolves this: use a Count-Min Sketch as an approximate frequency estimator, then admit a new item only if its estimated frequency exceeds that of the entry it would evict.

The production implementation in **Caffeine** (and Rust's `moka`) sharpens the sketch for the cache setting:

- **4-bit counters.** Access frequencies are small and long-tailed, so counters saturate at 15 — packing 16 counters per 64-bit word, costing roughly *4× the cache capacity in counters* rather than a full `int` per key.
- **Aging by halving.** After a sample window of `10 × capacity` operations, *every* counter is divided by two. This is the crucial addition to plain Count-Min: it lets stale popularity fade so the cache tracks the *current* hot set instead of anchoring to keys that were hot an hour ago. The halving also reclaims headroom in the 4-bit counters.
- **W-TinyLFU** front-runs this with a small LRU *window* to catch bursty newcomers the sketch hasn't learned yet, then has the window's victim and the main region's probation candidate compete on estimated frequency — higher estimate wins, with a little random jitter to defeat hash-collision attacks.

The one-sided error is what makes this safe: because the sketch never *under*counts, a truly hot key's estimate can't fall below its real frequency and get wrongly evicted; the worst case is a lukewarm key looking slightly hotter than it is.

## Beyond admission: hot-key and heavy-hitter detection

The same estimator finds **heavy hitters** — keys whose frequency exceeds some fraction of the stream. Feed every request into a sketch, and any key whose estimate crosses a threshold is a hot-key candidate (the no-underestimate property means you never miss one; you only get occasional false positives to filter). That's the trigger for operational responses: replicate a hot partition across more nodes, promote a hotspot into a dedicated local cache, or shed load for an abusive client. Redis ships this directly as the `CMS.*` commands in RedisBloom.

## Count-Min vs HyperLogLog vs Bloom

Three sketches, three questions, easy to confuse:

- **Count-Min Sketch** — *how often* is key X? Frequency, one-sided overestimate.
- **[HyperLogLog](/articles/distributed-systems/2026-08-10-hyperloglog-cardinality-estimation)** — *how many distinct* keys? Cardinality, ~2% two-sided error, merges losslessly.
- **Bloom / Cuckoo filters** — *have I seen* key X at all? Set membership, no counts.

Count-Min is the only one of the three that estimates *counts*, and that's precisely why frequency-aware caches like TinyLFU reach for it. (Bloom, Cuckoo, and TinyLFU each have their own article in this series.)

**Try next:** run the implementation above, then compare `add` versus `add_conservative` on a Zipfian key stream — plot estimated-vs-true frequency for a few keys and watch the conservative variant hug the diagonal far more tightly, then add the periodic halving and confirm a once-hot key's estimate decays as the workload shifts.
