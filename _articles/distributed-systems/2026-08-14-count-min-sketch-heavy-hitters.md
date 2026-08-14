---
title: "Count-Min Sketch: Frequency Estimation and Heavy Hitters in Sublinear Space"
date: 2026-08-14
track: distributed-systems
summary: "A d×w grid of counters that estimates how often you've seen any item in a stream, never underestimates, and fits the error to a knob: w=⌈e/ε⌉ columns and d=⌈ln 1/δ⌉ rows."
reading_time: 5
tags: [count-min-sketch, probabilistic-data-structures, streaming, heavy-hitters, frequency-estimation]
sources:
  - title: "An Improved Data Stream Summary: The Count-Min Sketch and its Applications — Cormode & Muthukrishnan (J. Algorithms, 2005)"
    url: "http://dimacs.rutgers.edu/~graham/pubs/papers/cm-full.pdf"
  - title: "Count-Min Sketch — Graham Cormode's project page"
    url: "https://sites.google.com/site/countminsketch/"
  - title: "Count-Min Sketch: The Art and Science of Estimating Stuff — Redis Blog"
    url: "https://redis.io/blog/count-min-sketch-the-art-and-science-of-estimating-stuff/"
  - title: "Count-min sketch — Redis Documentation (CMS commands)"
    url: "https://redis.io/docs/latest/develop/data-types/probabilistic/count-min-sketch/"
---

## The problem: "how many times have I seen X?" without a hash map

You have a firehose — clicks, IP packets, search terms, API keys — and you want per-item counts: which URLs are hot, which source IP is flooding you. The exact answer is a hash map from item to counter, but that costs `O(number of distinct items)` memory. With billions of distinct keys, or millions of keys per shard per minute, the map is the bottleneck. Most of those keys are seen once and never matter.

The Count-Min Sketch (Cormode & Muthukrishnan, 2005) trades exactness for a fixed, tiny footprint. It answers "roughly how many times have I seen item X?" from a small 2-D array of counters, and it never *under*-counts — a property that turns out to be exactly what heavy-hitter detection needs.

## The structure: d rows × w columns

The sketch is a grid `count[d][w]` of integer counters, all starting at zero, plus `d` independent hash functions `h_1 … h_d`, each mapping any item into `[0, w)`.

- **Update(x, c):** for each row `j`, bump `count[j][h_j(x)] += c`. Every item touches exactly one cell per row — `d` increments total.
- **Query(x):** read the `d` cells item `x` maps to and return the **minimum**: `min_j count[j][h_j(x)]`.

Why the minimum? Every cell `x` lands in has been incremented at least once for every occurrence of `x`, so no cell can be *too low*. The only error is other items colliding into the same cell and inflating it. Taking the min keeps the least-polluted estimate, so the answer is always `≥` the true count and biased only upward.

## The error knobs: ε and δ

Here's the elegant part — you dial the two dimensions from two error targets. Choose an accuracy `ε` and a failure probability `δ`, then set:

```
w = ⌈e / ε⌉        # columns  (e ≈ 2.718)
d = ⌈ln(1 / δ)⌉    # rows
```

The paper's guarantee: the estimate `â_x` satisfies `a_x ≤ â_x`, and with probability at least `1 − δ`,

```
â_x ≤ a_x + ε · ‖a‖₁
```

where `‖a‖₁` is the total count over all items (the stream length). In words: the overestimate is at most `ε` times the total volume, and that bound holds all but a `δ` fraction of the time. Total space is `O((1/ε) · ln(1/δ))` counters — independent of how many distinct items you've seen. To halve the error you double the columns; to make failures ten times rarer you add `ln 10 ≈ 2.3` rows. The Redis blog gives a concrete feel: depth 10, width 2,000, one million items → each counter averages `1M / 2K = 500` of noise, so counts well above 500 are trustworthy and the "mouse flows" below it are lost in the grass.

## Thirty lines of Python

```python
import math, random

class CountMinSketch:
    def __init__(self, epsilon=0.001, delta=0.001):
        self.w = math.ceil(math.e / epsilon)      # columns
        self.d = math.ceil(math.log(1 / delta))   # rows
        self.count = [[0] * self.w for _ in range(self.d)]
        # d independent hashes via random salts
        rng = random.Random(42)
        self.salts = [rng.getrandbits(64) for _ in range(self.d)]

    def _cells(self, item):
        h = hash(item)
        for j, salt in enumerate(self.salts):
            yield j, (h ^ salt) % self.w

    def update(self, item, c=1):
        for j, col in self._cells(item):
            self.count[j][col] += c

    def query(self, item):
        return min(self.count[j][col] for j, col in self._cells(item))

cms = CountMinSketch(epsilon=0.001, delta=0.001)
for _ in range(10_000): cms.update("elephant")
for i in range(50_000):  cms.update(f"mouse-{i}")   # noise
print(cms.query("elephant"))   # >= 10000, slightly inflated
print(cms.query("never-seen")) # small: only collision noise
```

(For production, use two well-mixed hashes combined as `h1 + j*h2` rather than XOR salts — but the shape is exactly this.)

## Heavy hitters: who's above the threshold?

A **heavy hitter** is any item whose frequency exceeds some fraction `φ` of the total, e.g. every IP responsible for more than 1% of packets. The sketch is a natural fit because it never underestimates: if `query(x) < φ‖a‖₁`, then `x` is *definitely* not a heavy hitter and can be discarded. You only need to keep candidates whose estimate crosses the bar.

The standard construction pairs the sketch with a small min-heap of the current top-K. On each update, query the item; if its estimate beats the heap's smallest, push it in and evict. The sketch handles the "count everything in bounded space" half; the heap handles the "remember only the winners" half. This is the Top-K pattern the Redis blog describes for finding the top player of a game or the trending item, and it's why the structure shows up in DDoS detection, database query-plan statistics, and stream analytics.

Redis ships it natively via RedisBloom. You size it by error targets, not dimensions:

```
CMS.INITBYPROB traffic 0.001 0.002    # error ε=0.1%, prob δ=0.2%
CMS.INCRBY     traffic "1.2.3.4" 100  # add 100 hits for that IP
CMS.QUERY      traffic "1.2.3.4"      # estimated count (never below true)
```

Two more properties make it distributed-systems-friendly: sketches with the same dimensions and hashes **merge by element-wise addition** (`CMS.MERGE`), so per-shard sketches roll up into a global one; and every operation is `O(d)`, constant regardless of stream size.

**Try next:** build the Python sketch above, feed it a Zipfian stream, and plot estimated vs. true counts. Sweep `w` and watch the overestimate track `ε·‖a‖₁`; then add a min-heap on top and verify it recovers the true top-10 even when tens of thousands of rare items are colliding into the grid.
