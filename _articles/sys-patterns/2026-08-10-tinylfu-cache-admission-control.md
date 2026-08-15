---
title: "Cache Admission Control with TinyLFU: Deciding What to Let In"
date: 2026-08-10
track: sys-patterns
summary: "Eviction answers only half the question. Least-recently-used (LRU) eviction admits every new item unconditionally, discarding a resident that may be more valuable. TinyLFU adds an admission filter: a Count-Min frequency sketch with 4-bit counters, a doorkeeper bloom filter for one-hit-wonders, and an aging reset that halves all counters to track recent popularity. This article covers the admit() decision, the aging step, and why Window-TinyLFU (Caffeine) places a small LRU window in front to survive bursts and scans."
reading_time: 7
tags:
  - caching
  - admission-control
  - tinylfu
  - w-tinylfu
  - count-min-sketch
  - bloom-filter
  - interview-prep
sources:
  - title: "Einziger, Friedman & Manes — TinyLFU: A Highly Efficient Cache Admission Policy (arXiv:1512.00727)"
    url: "https://arxiv.org/abs/1512.00727"
  - title: "Einziger, Friedman & Manes — TinyLFU (ACM Transactions on Storage, Vol. 13 No. 4, 2017)"
    url: "https://dl.acm.org/doi/10.1145/3149371"
  - title: "Caffeine Wiki — Design (Window TinyLFU, FrequencySketch)"
    url: "https://github.com/ben-manes/caffeine/wiki/Design"
  - title: "Ben Manes — Design of a Modern Cache (High Scalability)"
    url: "https://highscalability.com/design-of-a-modern-cache/"
  - title: "Caffeine Wiki — Efficiency (hit-rate simulations vs LRU/ARC/LIRS)"
    url: "https://github.com/ben-manes/caffeine/wiki/Efficiency"
---

**Gist.** Least-recently-used (LRU) caching decides only which resident leaves, never whether the arrival deserves a seat, so a single pass over cold keys evicts the entire working set. TinyLFU adds an **admission filter**: an approximate frequency estimate for the candidate is compared against the estimate for the victim the eviction policy has already nominated, and the candidate is admitted only if it wins. The cost is a frequency sketch that must be maintained on every access and periodically aged, plus the risk that an item with genuine future value is rejected before it has accumulated enough observed frequency to win the comparison.

## The admission decision

Eviction policy and admission policy are separable. The eviction policy — LRU, segmented LRU (SLRU), or another — still nominates a victim. TinyLFU sits in front of it as a filter:

```
function admit(candidate):
    victim = evictionPolicy.chooseVictim()
    if estimate(candidate) > estimate(victim):
        return ADMIT      # newcomer estimated more valuable; evict victim
    else:
        return REJECT     # keep victim; do not cache candidate
```

The consequence for scans is structural rather than heuristic. A key touched once during a sequential pass has `estimate(candidate)` of 1 or 0, while a resident of the working set has a materially higher estimate; the comparison fails and the resident survives. **Scan resistance therefore falls out of frequency-based admission without adopting least-frequently-used (LFU) eviction**, and so without LFU's unbounded per-key counters or its inability to forget.

The design depends entirely on `estimate()` being simultaneously accurate enough to order two keys and small enough to be worth keeping. An exact per-key frequency table would cost more memory than the cache it protects, so TinyLFU approximates.

## The frequency sketch: Count-Min with 4-bit counters

`estimate()` is backed by a **Count-Min sketch** (a matrix of counters, `d` hash functions, a per-key read that takes the minimum over rows — see the [Count-Min sketch article](/articles/distributed-systems/2026-08-10-count-min-sketch) for the derivation). Two adaptations distinguish the caching variant.

- **4-bit counters.** The policy never needs an absolute access total; it needs an ordering between two keys. A counter that **saturates at 15** suffices. Caffeine's `FrequencySketch` uses a 4-bit Count-Min sketch, packing counters densely so the whole sketch costs **roughly 8 bytes per cache entry**.
- **Minimal increment.** On access, TinyLFU reads all `d` counters for the key and increments **only the smallest one or ones**. For counters reading `{2, 2, 5}`, the two 2s become 3 and the 5 is left alone. Since a Count-Min sketch reads the minimum, incrementing a counter already above the minimum would only add collision noise; withholding that increment suppresses part of the sketch's systematic over-count and keeps rare-key estimates from inflating toward the popular keys they collide with.

`estimate(key)` returns the minimum across the key's `d` counters, as in the unmodified Count-Min sketch.

## The doorkeeper

Skewed workloads have a long tail: most distinct keys are observed **once** inside a measurement window. Allocating a multi-bit sketch counter in every row to a key that never recurs is waste. The paper's remedy is a **doorkeeper** — a plain bloom filter placed in front of the sketch.

The paper describes the doorkeeper as a regular bloom filter placed in front of the approximate counting scheme: on item arrival the doorkeeper is checked first; an item absent from it — the expected case for first timers and tail items — is inserted into the doorkeeper, and otherwise into the main structure.

A key's **first** sighting therefore flips bits only in the doorkeeper and never touches the sketch; its **second** sighting promotes it to the main structure. Estimation combines the two: **if the key is present in the doorkeeper, 1 is added to whatever the main sketch reports**.

```
function estimate(key):
    e = sketch.estimate(key)          # min over d counters
    if doorkeeper.contains(key):
        e = e + 1
    return e

function record(key):                 # called on every access
    if doorkeeper.contains(key):
        sketch.increment(key)         # seen before -> real counter
    else:
        doorkeeper.add(key)           # first-timer -> 1 bit only
    onIncrement()                     # drive the aging clock
```

The paper's stated effect is that tail items are allocated a single bit in the doorkeeper rather than a multi-bit counter, which on skewed workloads reduces the memory TinyLFU consumes. A one-hit-wonder costs a single bit per bloom hash and, with an estimate of 1, loses the `admit()` comparison against any warm resident.

## Aging: periodic halving

An LFU estimator that never forgets preserves yesterday's popular key at its peak count indefinitely, and that key then wins every admission comparison against a currently rising key. TinyLFU bounds the memory of the estimator with a reset step. A running counter tracks total increments; on reaching the **sample size `W`**, every counter in the sketch is **divided by two** and the doorkeeper is cleared.

```
sampleSize = W               # e.g. ~ 10x cache capacity
count = 0

function onIncrement():
    count += 1
    if count >= sampleSize:
        reset()

function reset():
    for c in sketch.counters:
        c = c >> 1           # halve (integer divide by 2)
    doorkeeper.clear()       # tail estimates start fresh
    count = count >> 1       # the increment tally halves with the counters
```

Two properties follow. **Halving preserves ratios**, so the relative ordering that `admit()` consumes survives the reset while absolute magnitudes shrink; and **4-bit counters cannot overflow**, because the reset periodically pulls them back below saturation. The estimator consequently behaves as a **windowed** LFU — decay with a half-life of one sample window — rather than an all-time LFU. Integer truncation is the visible imprecision: a counter of 1 halves to 0, so a key observed exactly once before the reset is indistinguishable afterwards from a key never observed at all.

## Window-TinyLFU

Admission by frequency alone rejects an item that has genuine value but no accumulated history: an item hit several times in quick succession and then again much later may fail `admit()` before the sketch has recorded the burst. Caffeine's **Window-TinyLFU (W-TinyLFU)** addresses this with a two-region layout.

- A small **admission window**, managed as LRU (historically around 1% of capacity). Every new item enters here first, unfiltered.
- A large **main region** (around 99%), managed as **SLRU**: a probation segment plus a protected segment capped near 80% of the main space — see [eviction policies](/articles/sys-patterns/2026-08-10-cache-eviction-policies).

When the window evicts its LRU victim, that victim is not discarded; it becomes the **candidate** for the main region, and the TinyLFU filter runs only at that boundary: `estimate(windowVictim) > estimate(mainVictim)` decides admission. **The window absorbs recency and bursts, and the frequency filter guards the durable working set against scans.** Caffeine additionally **adapts the window/main split at runtime by hill climbing on the observed hit rate**, enlarging the window for recency-heavy workloads and shrinking it for frequency-biased ones. The [Caffeine article](/articles/scala-jvm/2026-08-10-caffeine-w-tinylfu-caching) covers the JVM-side application programming interface (API).

## Where the hit-ratio gain comes from

Under uniform-random access, admission control contributes nothing: all keys are equally unlikely to recur, and the sketch is pure overhead. The gain appears on the two workload shapes real caches encounter.

- **Skewed (Zipfian) popularity.** The sketch separates head from tail at low cost; `admit()` protects the head and the doorkeeper keeps the tail from consuming counters. Caffeine's published simulations show W-TinyLFU tracking near-optimal hit rates on workloads where LRU trails.
- **Scan and loop workloads.** A single pass over cold keys cannot win the frequency comparison against a warm resident, so it is rejected at the door rather than evicting the working set. LRU, having no admission stage, has no defence.

The separation worth retaining: **eviction selects who leaves; admission decides whether the arrival has earned the seat.** LRU implements only the first and treats every arrival as deserving. TinyLFU makes admission a frequency argument at a cost of roughly 8 bytes per entry.

### Implementation sketch (Scala)

A 4-bit Count-Min sketch with minimal increment and the halving reset. Counters are packed 16 to a `Long`; the doorkeeper and the eviction policy are omitted.

```scala
final class FrequencySketch(capacity: Int, sampleSize: Int):
  private val tableMask = Integer.highestOneBit(math.max(capacity, 1)) * 2 - 1
  private val table = Array.fill((tableMask + 1) / 16 + 1)(0L)
  private val seeds = Array(0x9E3779B9, 0x85EBCA6B, 0xC2B2AE35, 0x27D4EB2F)
  private var size = 0

  private def indexOf(hash: Int, row: Int): Int =
    val h = hash * seeds(row)
    (h ^ (h >>> 16)) & tableMask

  /** Minimum over the d rows: the Count-Min read. */
  def frequency(hash: Int): Int =
    (0 until 4).map { row =>
      val i = indexOf(hash, row)
      ((table(i >>> 4) >>> ((i & 15) << 2)) & 0xFL).toInt
    }.min

  /** Increments only the counters currently at the minimum. */
  def increment(hash: Int): Unit =
    val slots = (0 until 4).map(indexOf(hash, _))
    val values = slots.map(i => ((table(i >>> 4) >>> ((i & 15) << 2)) & 0xFL).toInt)
    val min = values.min
    if min < 15 then                            // 4-bit counters saturate at 15
      slots.zip(values).foreach { case (i, v) =>
        if v == min then
          val shift = (i & 15) << 2
          table(i >>> 4) += (1L << shift)
      }
      size += 1
      if size >= sampleSize then reset()

  /** Halving preserves the ordering admit() reads, and bounds the counters. */
  private def reset(): Unit =
    var j = 0
    while j < table.length do
      table(j) = (table(j) >>> 1) & 0x7777777777777777L  // shift each nibble, mask carries
      j += 1
    size = size >>> 1
```

## Pitfalls

- **Halving loses single observations.** A counter at 1 becomes 0 at the reset, so a key seen exactly once in the previous window is treated as never seen; a periodic workload whose period exceeds the sample window `W` is repeatedly rejected at admission.
- **`admit()` without a window starves sparse bursts.** An item hit several times in close succession and then again much later has no accumulated sketch frequency at the moment of the comparison and is rejected before it can prove itself — the defect W-TinyLFU's admission window exists to cover.
- **A sample size that is too large freezes the estimate.** Reset then occurs rarely, old popularity keeps winning comparisons, and hit ratio degrades slowly and without an obvious symptom beyond newly popular keys failing to enter the cache.
- **Incrementing all `d` counters instead of the minimum inflates rare keys.** Without minimal increment, a cold key colliding with a hot one in one row inherits collision mass, its estimate rises, and it starts winning admissions it should lose.
- **A doorkeeper that is never cleared saturates.** The bloom filter's false-positive rate grows with insertions; once most lookups return true, every key gains the +1 doorkeeper bonus and the bonus stops carrying information. The clear must happen at each reset.
- **Admission control is overhead on uniform workloads.** With no popularity skew, estimates are indistinguishable, the filter's decisions are arbitrary, and the sketch's per-access work and roughly 8 bytes per entry buy no hit-ratio improvement.
