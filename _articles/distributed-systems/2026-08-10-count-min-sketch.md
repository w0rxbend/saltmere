---
title: 'The Count-Min Sketch: Frequency Estimation in Sublinear Space'
date: 2026-08-10
track: distributed-systems
summary: How a two-dimensional array of counters and d hash functions estimates key frequencies in kilobytes instead of gigabytes, why the estimator overestimates but never underestimates, the w = ⌈e/ε⌉ error bound, the conservative-update variant, and how the sketch underpins TinyLFU cache admission and hot-key detection.
reading_time: 7
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

**Gist.** Answering "how many times has this key appeared?" exactly requires one counter per *distinct* key, and for URLs, IP addresses or user identifiers that population runs to billions. The **Count-Min Sketch** (Cormode & Muthukrishnan, 2005) replaces the map with a fixed `d × w` array of counters and `d` hash functions: an update increments one counter per row, a query returns the minimum of those `d` counters. The cost is a **one-sided error** — the estimate is never below the true count, but with probability at least `1 − δ` it exceeds the truth by no more than `ε · ‖a‖₁`, an error scaled by the *total stream size* rather than by the key's own frequency.

## The problem: frequencies that cannot be stored exactly

Several classes of system need per-key frequencies. A cache needs to know whether a candidate is accessed often enough to be worth retaining. A load balancer needs to identify the single product identifier absorbing a disproportionate share of requests. A network device needs the flows saturating a link. The exact answer is a hash map from key to count, and that map grows with the number of distinct keys, most of which appear once and never recur.

Count-Min trades exactness for a footprint fixed in advance. Like HyperLogLog it is a *sketch* — a small summary that items are fed into and queried against — but it answers a different question. HyperLogLog estimates *cardinality*, how many distinct elements occurred; Count-Min estimates *frequency*, how often a nominated element occurred. (See the companion article on [HyperLogLog](/articles/distributed-systems/2026-08-10-hyperloglog-cardinality-estimation).)

## The structure: d rows, w counters, one increment per row

A Count-Min Sketch is a two-dimensional counter array of **depth `d`** (rows) and **width `w`** (columns), together with `d` hash functions `h₁…h_d` drawn from a pairwise-independent family, each mapping a key into `{1…w}`.

- **Add `(key, c)`:** for every row `j`, `table[j][h_j(key)] += c`. **One counter per row, so `O(d)` work independent of `w`.**
- **Estimate `(key)`:** return the minimum over rows of `table[j][h_j(key)]`.

Each row is an independent lossy histogram: distinct keys collide into a shared column and their counts accumulate together. Because the rows use different hash functions, two keys colliding in row 1 are unlikely to collide again in row 2. The minimum across rows therefore selects the row in which the queried key absorbed the least collision noise.

## Why the estimator overestimates but never underestimates

Every increment for `key` lands on the `d` counters that `key` hashes to, so each of those counters is at least the true count of `key`. Increments from *other* keys can only raise those counters. Each row therefore yields an overestimate, and the minimum of overestimates remains at least the truth:

```
estimate(key) ≥ true_count(key)     always
```

The sketch is a **one-sided estimator**: it can report a key as hotter than it is, but it cannot conceal a hot key. That asymmetry is the property admission control and heavy-hitter detection depend on — a false positive is a lukewarm key admitted, whereas a false negative would be a genuinely hot key rejected, which the structure cannot produce.

## The error bound

Both dimensions follow directly from the target additive error `ε` and failure probability `δ`:

```
w = ⌈e / ε⌉          (e = Euler's number ≈ 2.718)
d = ⌈ln(1 / δ)⌉
```

The guarantee stated in the paper: the estimate never underestimates, and **with probability at least `1 − δ`,**

```
estimate(key) ≤ true_count(key) + ε · ‖a‖₁
```

where `‖a‖₁` is the L1 norm of the frequency vector, that is the total number of items added. **The error is additive and proportional to the total stream size, not to the queried key's own count** — so a key occurring once in a stream of 10⁹ items carries the same absolute error budget as a key occurring 10⁶ times, which makes the estimator far more useful for heavy hitters than for rare keys. Increasing `w` shrinks `ε`, the overcount per query; increasing `d` drives down `δ`, the probability that every row was simultaneously unlucky. Space is `O(w·d)` counters, `O((1/ε)·ln(1/δ))`, **independent of the number of distinct keys**.

With `ε = 0.001` and `δ ≈ 0.001`: `w = ⌈e/0.001⌉ = 2719`, `d = ⌈ln 1000⌉ = 7`, 19,033 counters — under 80 KB at 32-bit counters, and a few kilobytes if the counters are narrowed to 4 or 8 bits — estimating any key's frequency in a billion-item stream to within 0.1% of the stream size, with probability at least 99.9%.

### Implementation sketch (Scala)

```scala
final class CountMinSketch(val w: Int = 2719, val d: Int = 7):
  private val table: Array[Array[Long]] = Array.ofDim[Long](d, w)

  // d columns from one hash: seed each row differently and fold to [0, w)
  private def columns(key: String): Array[Int] =
    Array.tabulate(d): j =>
      val h = scala.util.hashing.MurmurHash3.stringHash(key, 0x9747b28c + j)
      math.floorMod(h, w)

  def add(key: String, count: Long = 1L): Unit =
    val cols = columns(key)
    var j = 0
    while j < d do
      table(j)(cols(j)) += count
      j += 1

  def estimate(key: String): Long =
    val cols = columns(key)
    (0 until d).map(j => table(j)(cols(j))).min

  /** Conservative update: raise counters only up to the new minimum. */
  def addConservative(key: String, count: Long = 1L): Unit =
    val cols = columns(key)
    val target = (0 until d).map(j => table(j)(cols(j))).min + count
    var j = 0
    while j < d do
      if table(j)(cols(j)) < target then table(j)(cols(j)) = target
      j += 1
```

Seeding one hash primitive `d` times supplies the `d` hash functions without instantiating `d` separate hashers.

## The conservative-update variant

Plain `add` increments every counter belonging to a key. Since a query reads only the minimum, the counters already above that minimum need not grow. **Conservative update** — Estan & Varghese's "minimal increment" — computes the post-increment estimate `current_min + count` and raises each of the key's counters only as far as that target, leaving larger counters untouched.

The no-underestimate guarantee survives, and the overcount falls, because counters shared with other keys stop absorbing increments that would not change the reported minimum. The cost is that **the variant is no longer a linear sketch**: two conservatively updated sketches over disjoint streams cannot be merged by counter-wise addition, which removes the parallel-aggregation property that plain Count-Min has. Worst-case behaviour of the variant is analysed in the arXiv paper cited below.

## TinyLFU admission

Least-recently-used (LRU) eviction admits every accessed item, so a single scan of one-hit keys can displace genuinely popular entries. Least-frequently-used (LFU) eviction retains popular entries but classically requires a real counter per key — the cost the sketch exists to avoid. **TinyLFU** (Einziger, Friedman & Manes, 2017) combines the two: a Count-Min Sketch supplies approximate frequencies, and a new item is admitted only when its estimated frequency exceeds that of the entry it would displace.

The implementation in **Caffeine** (and Rust's `moka`) adapts the sketch to the cache setting:

- **4-bit counters** saturating at 15, packing 16 counters per 64-bit word, so the sketch costs a few bits per tracked entry rather than a full-width integer per key.
- **Aging by halving.** After a sample window of `10 × capacity` operations, every counter is divided by two, so past popularity decays and the estimator tracks the current hot set. The halving also reclaims headroom in the 4-bit counters.
- **W-TinyLFU** places a small LRU *window* in front, catching bursty newcomers the sketch has not yet observed; the window's victim and the main region's probation candidate then compete on estimated frequency, with random jitter in the comparison to resist hash-collision attacks.

The one-sided error is what makes the arrangement safe: since the sketch cannot undercount, a hot key's estimate cannot fall below its real frequency, and the worst outcome is a lukewarm key appearing slightly hotter than it is.

## Heavy-hitter and hot-key detection

The same estimator identifies **heavy hitters**, keys whose frequency exceeds a chosen fraction of the stream. Every request is added to a sketch, and any key whose estimate crosses the threshold becomes a candidate; the no-underestimate property means no heavy hitter is missed, and the residual errors are false positives that a second pass can filter. Detected hot keys are the trigger for operational responses such as replicating a hot partition, promoting the key into a dedicated local cache, or shedding load. Redis exposes the structure directly through the `CMS.*` commands in RedisBloom.

## Count-Min versus HyperLogLog versus Bloom

- **Count-Min Sketch** — how often does key X occur? Frequency, one-sided overestimate.
- **[HyperLogLog](/articles/distributed-systems/2026-08-10-hyperloglog-cardinality-estimation)** — how many distinct keys occur? Cardinality, two-sided relative error set by the register count, merges losslessly.
- **Bloom and Cuckoo filters** — has key X occurred at all? Set membership, no counts.

Count-Min is the only one of the three that estimates counts, which is why frequency-aware admission policies such as TinyLFU are built on it.

## Pitfalls

- **Reading an absolute error into a low-frequency key.** The bound is `ε · ‖a‖₁`, scaled by the whole stream; a key seen twice in a stream of 10⁹ items can be reported as thousands of occurrences, so per-key estimates are meaningful only for keys whose true counts are large relative to `ε · ‖a‖₁`.
- **Merging conservatively updated sketches counter-wise.** Conservative update is not linear, so summing two such sketches yields a result with no proved bound; only plain Count-Min sketches of identical `w`, `d` and hash seeds merge by addition.
- **Sketches with mismatched dimensions or seeds.** Counter-wise merging assumes both sketches map each key to the same columns; differing `w`, `d` or hash seeds produce silently meaningless counts rather than an error.
- **Never aging counters in a long-lived process.** Plain Count-Min accumulates monotonically, so a key hot an hour ago keeps its high estimate forever; TinyLFU's periodic halving exists because the unaged sketch reports historical rather than current popularity.
- **Saturating narrow counters.** Caffeine's 4-bit counters stop at 15, so all keys above that frequency become indistinguishable; the comparison degrades to a tie once the hot set exceeds the counter ceiling and no halving has intervened.
- **Treating the estimate as an exact count in downstream arithmetic.** Subtracting two estimates, or dividing one by another, compounds two independent overestimates and can yield a ratio far from the true one; the guarantee covers single-key queries only.
- **Assuming deletions work.** Decrements break the no-underestimate invariant, because a decrement applied to a shared counter also removes count belonging to colliding keys; the standard sketch supports non-negative updates only.
