---
title: "Cache Eviction at Scale: LRU, LFU, and Why Caffeine Ships W-TinyLFU"
date: 2026-08-15
track: distributed-systems
summary: "Least-recently-used eviction discards the hottest keys when a scan floods the cache; least-frequently-used eviction retains yesterday's winners indefinitely. W-TinyLFU combines both signals by placing a Count-Min frequency sketch in front of an admission window and a segmented LRU. It is the policy Caffeine ships, and Go's Ristretto uses a TinyLFU admission filter of the same family. This article describes the policy and the admission decision it turns on."
reading_time: 6
tags: [caching, eviction, lru, lfu, w-tinylfu, count-min-sketch]
sources:
  - title: "TinyLFU: A Highly Efficient Cache Admission Policy (Einziger, Friedman, Manes)"
    url: "https://arxiv.org/abs/1512.00727"
  - title: "Caffeine Wiki — Efficiency (W-TinyLFU design)"
    url: "https://github.com/ben-manes/caffeine/wiki/Efficiency"
  - title: "Design of a Modern Cache—Part Deux (Ben Manes)"
    url: "https://highscalability.com/design-of-a-modern-cachepart-deux/"
  - title: "Caffeine — high performance caching library for Java"
    url: "https://github.com/ben-manes/caffeine"
  - title: "Ristretto — high performance memory-bound Go cache (v2)"
    url: "https://pkg.go.dev/github.com/dgraph-io/ristretto/v2"
---

**Gist.** A cache replacement policy that reads only one signal fails on the workload that carries the other: least-recently-used (LRU) eviction is emptied by a single sequential scan, and least-frequently-used (LFU) eviction retains items whose popularity has already collapsed. **Window TinyLFU (W-TinyLFU)** keeps both signals by admitting entries through a frequency filter backed by an approximate, periodically aged Count-Min Sketch, with a small LRU admission window in front of a segmented main region. The cost is a probabilistic frequency estimate that can be wrong under hash collisions, plus the memory and moving parts of a sketch and a two-segment main region that a plain LRU does not carry.

## The classic policies and their failure modes

**First-in first-out (FIFO)** evicts in insertion order and consults no access information, so a hot key inserted early is evicted on schedule regardless of its access rate.

**LRU** evicts the least-recently-used entry. It captures recency and temporal locality at O(1) per access, but retains no record of *how often* an entry was used. Two failure modes follow directly from that omission. A **scan** — a burst of keys never accessed again — touches every position in the recency order and therefore evicts the entire resident set. **One-hit wonders**, keys touched exactly once, are admitted unconditionally, and each admission costs the eviction of an incumbent that may be genuinely hot.

**LFU** evicts the least-frequently-used entry and is consequently resistant to scans: a scanned key has a frequency of one and loses to any warm incumbent. Its failure mode is **aging**. Without a decay mechanism, counters are monotonically non-decreasing, so an item that was popular during a traffic spike remains resident long after its access rate falls. Exact LFU also requires a counter per key and a structure that yields the minimum, typically a heap at O(log n) per update, which is expensive at the entry counts where cache policy matters.

| Workload | FIFO | LRU | LFU | W-TinyLFU |
|---|---|---|---|---|
| Recency-biased (temporal locality) | poor | strong | weak | strong |
| Frequency-biased (Zipfian popularity) | poor | weak | strong | strong |
| Large scan / sequential flood | poor | fails | resists | resists |
| One-hit wonders | poor | admits, evicts an incumbent | evicts them first | rejects at admission |
| Shifting popularity over time | poor | adapts fast | ages badly | adapts (sketch resets) |

## W-TinyLFU: a sketch in front of a segmented LRU

W-TinyLFU, described by Einziger, Friedman and Manes alongside TinyLFU itself, splits the cache in two and gates the boundary with a frequency filter.

- A small **admission window**, a plain LRU holding roughly **1% of capacity**, absorbs brand-new keys and recency bursts.
- A large **main region**, a **Segmented LRU (SLRU)** with a *probation* and a *protected* segment, holds entries that have earned residency.
- A **TinyLFU admission filter** decides whether an entry evicted from the window may displace the main region's eviction victim.

The filter's frequency estimate comes from a **Count-Min Sketch**: a table of **4-bit counters** — Caffeine packs sixteen of them into each 64-bit word and sizes the table at one word per cache entry — that estimates how often a key has been seen without storing the keys. Because counters are shared between keys, the estimate is an **overestimate**: a lookup returns the minimum across the key's counters, which is at least the true count and can be inflated by collisions, never deflated.

The load-bearing addition over plain LFU is **aging**. A running counter tracks total increments, and when it reaches a **sample size proportional to cache capacity, every counter is halved**. That periodic reset gives frequency information a fading memory, so a key that was hot during an earlier spike decays out rather than pinning itself.

## The admission decision

When the window overflows, its LRU victim — the *candidate* — is not admitted automatically. The filter compares the candidate's estimated frequency against that of the main region's eviction victim, taken from the SLRU probation tail. **The higher estimated frequency wins**: if the candidate's estimate exceeds the victim's, the candidate is admitted and the victim evicted; otherwise the incumbent is kept and the candidate discarded.

This is the invariant that makes scans survivable. Each scanned key carries an estimated frequency near one and loses the comparison against any warm incumbent, so the scan never propagates past the admission window; the damage is bounded by the window's size rather than by the cache's.

An exact comparison has an adversarial weakness: an attacker who can manufacture frequency ties can freeze cache contents. Caffeine's defence is a **randomised branch** — when frequencies are close but non-trivial, a candidate that lost the comparison is admitted with a small fixed probability, so contents cannot be pinned by forged near-ties.

Caffeine additionally uses an **adaptive window**: hill climbing resizes the window-versus-main split at runtime, sampling hit rate and stepping toward the ratio the current workload favours. The window grows under recency-heavy load and shrinks under frequency-heavy load.

### Implementation sketch (Scala)

```scala
final class TinyLfuSketch(capacity: Int, depth: Int = 4):
  private val width  = Integer.highestOneBit(math.max(capacity, 1)) * 2
  private val table  = Array.ofDim[Int](depth, width)
  private val sample = capacity * 10          // increments before halving
  private var additions = 0

  private def index(hash: Int, row: Int): Int =
    (hash * (row * 2 + 1) >>> 8) & (width - 1)

  /** Minimum across rows: never below the true count, possibly above it. */
  def frequency(key: Any): Int =
    val h = key.##
    (0 until depth).map(r => table(r)(index(h, r))).min

  def increment(key: Any): Unit =
    val h = key.##
    for r <- 0 until depth do
      val i = index(h, r)
      if table(r)(i) < 15 then table(r)(i) += 1   // saturate at 4-bit max
    additions += 1
    if additions >= sample then reset()

  /** Aging: halving every counter is what plain LFU lacks. */
  private def reset(): Unit =
    for r <- 0 until depth; i <- 0 until width do table(r)(i) >>>= 1
    additions = 0

// A loser is admitted with small probability only if its frequency is
// non-trivial, so near-ties cannot be forged to pin the cache contents.
val WarmEnough  = 5
val AdmitOdds   = 128

def admit(sketch: TinyLfuSketch, candidate: Any, victim: Any): Boolean =
  val c = sketch.frequency(candidate)
  if c > sketch.frequency(victim) then true
  else c > WarmEnough && scala.util.Random.nextInt(AdmitOdds) == 0
```

## Where the design runs

**Caffeine** (Java), by Ben Manes, is the reference implementation. Go's **Ristretto** (Dgraph) applies a TinyLFU admission policy built on a sketch preceded by a doorkeeper filter, and the Rust **Moka** cache adopted the design from Caffeine.

The trade-offs are bounded rather than free. The Count-Min Sketch is approximate, so estimates collide and can mislead on adversarially chosen keys. The sketch and the SLRU add memory and state that a small LRU does not require, and for a small cache or a purely recency-bound workload a plain LRU is simpler at comparable hit rate. W-TinyLFU repays its complexity when the cache is large, the key distribution is skewed, and scans or one-hit wonders would otherwise displace the working set.

Caffeine's bundled simulator (`caffeine/simulator`) runs a policy against a trace; comparing `Lru` and `WindowTinyLfu` hit ratios on a Zipfian trace and a sequential-scan trace exercises the admission filter's rejection of the scan directly.

## Pitfalls

- **Assuming the frequency estimate is exact.** The Count-Min Sketch returns a minimum over shared counters, which is an overestimate; a cold key colliding with a hot one is admitted although its true count is one.
- **Removing or lengthening the sketch reset interval.** Without halving at the sample size, frequency counters only grow, and the policy degenerates to the aging failure mode of plain LFU: entries popular during one spike remain resident indefinitely.
- **Sizing the admission window as though it were the cache.** The window is roughly 1% of capacity and holds keys that have not yet passed the filter; a workload whose reuse distance exceeds the window's size sees every reuse counted as a first access.
- **Benchmarking only on a Zipfian trace.** A frequency-skewed trace flatters any frequency-aware policy; the scan and one-hit-wonder cases are what separate W-TinyLFU from LRU, and they must be traced separately.
- **Expecting scan resistance to be absolute.** The scan is contained within the admission window, not eliminated; entries resident in the window when a scan begins are still evicted from it.
- **Treating the randomised admission branch as a hit-rate feature.** It exists to prevent contents being pinned by manufactured frequency ties, and it admits a small fraction of candidates that lost the comparison.
