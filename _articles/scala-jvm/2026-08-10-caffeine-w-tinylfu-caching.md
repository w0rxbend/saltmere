---
title: "Caffeine and W-TinyLFU: Near-Optimal In-Process Caching on the JVM"
date: 2026-08-10
track: scala-jvm
summary: "How Caffeine's Window TinyLFU (W-TinyLFU) policy reaches near-optimal hit rates on the Java Virtual Machine (JVM): the 4-bit Count-Min Sketch, the admission comparison, the window/probation/protected state machine, and the configuration trade-offs of LoadingCache, AsyncLoadingCache, expireAfterWrite versus refreshAfterWrite, and a Caffeine level-one cache in front of Redis."
reading_time: 9
tags:
  - caching
  - caffeine
  - jvm
  - scala
  - performance
sources:
  - title: "ben-manes/caffeine — A high performance, near optimal caching library (README)"
    url: "https://github.com/ben-manes/caffeine"
  - title: "Caffeine Wiki — Design (Window TinyLFU, CountMinSketch)"
    url: "https://github.com/ben-manes/caffeine/wiki/Design"
  - title: "Caffeine Wiki — Efficiency (hit-rate simulations vs LRU/ARC/LIRS)"
    url: "https://github.com/ben-manes/caffeine/wiki/Efficiency"
  - title: "Einziger, Friedman, Manes — TinyLFU: A Highly Efficient Cache Admission Policy (arXiv:1512.00727)"
    url: "https://arxiv.org/abs/1512.00727"
  - title: "TinyLFU — ACM Transactions on Storage, Vol 13, No 4 (2017)"
    url: "https://dl.acm.org/doi/10.1145/3149371"
---

**Gist.** A bounded in-process cache must decide, on every miss, which resident entry to discard, and least-recently-used (LRU) replacement answers that question using recency alone, so a single sequential scan of *n* cold keys expels the entire hot working set. Caffeine implements Window TinyLFU (W-TinyLFU), which adds an *admission* filter — an approximate frequency estimator built on a Count-Min Sketch (CMS) — so a candidate enters the main space only if its estimated historic usage exceeds that of the entry it would displace. The cost is roughly **8 bytes of sketch per cache entry**, an approximate (one-sided error) frequency estimate that can be inflated by hash collisions, and a periodic aging pass that halves every counter.

## Why recency alone loses hit rate

Let the resident set have capacity *c* and let the workload be a Zipfian key distribution plus an interleaved scan of *n* distinct keys touched once each. Under LRU, every scan key is admitted unconditionally on first reference, so after *c* scan references the resident set contains no hot key at all: the hit rate on the hot set drops to zero and must be re-earned by *c* compulsory misses. **LRU's defect is that it has no admission policy — reference count one and reference count one thousand are indistinguishable at insertion time.**

Least-frequently-used (LFU) replacement has the dual defect: exact LFU needs a counter per key ever observed, unbounded in space, and never forgets. The TinyLFU paper (Einziger, Friedman and Manes, arXiv:1512.00727; ACM Transactions on Storage 13(4), 2017) resolves both by making the frequency estimate *approximate* and *decaying*.

## The frequency sketch and its error bound

Caffeine maintains a **4-bit Count-Min Sketch**: *d* hash functions index *d* rows of 4-bit counters, an increment bumps all *d* counters (saturating at 15), and an estimate is the **minimum** of the *d* addressed counters. The minimum is the load-bearing choice. Every counter is the true count plus the counts of colliding keys, so each is an over-estimate; the minimum is therefore never below the true count. **The sketch has one-sided error: it never under-reports frequency, only over-reports it.** With *w* counters per row the classical CMS bound gives over-estimation of at most ε·N with probability 1 − δ for w = ⌈e/ε⌉ and d = ⌈ln(1/δ)⌉, where N is the number of increments in the current window.

Two adaptations make it fit in a cache. The table is provisioned against **maximum size, not against the key universe**, so the footprint is on the order of 8 bytes per admissible entry regardless of how many distinct keys the workload presents. And after a sample period proportional to the maximum size, every counter is **halved by a single shift**, which keeps N bounded so the ε·N error term does not drift upward and makes popularity decay exponentially.

## The admission state machine

An entry occupies exactly one of three regions:

- **Window** — a small LRU segment, default about 1% of capacity, holding every newly inserted entry.
- **Probation** — the cold segment of the main space, a Segmented LRU (SLRU).
- **Protected** — the hot segment of the main space, default about 80% of the main space, so roughly 79% of total capacity.

Transitions: insertion places a key at the window's most-recently-used end. When the window overflows, its LRU victim becomes a *candidate*. The candidate is not admitted unconditionally; TinyLFU compares `frequency(candidate)` against `frequency(victim)`, where the victim is the probation entry that would be evicted, and **retains the entry with the higher estimated historic usage** — the loser is discarded outright. A hit on a probation entry promotes it to protected; when protected overflows, its LRU victim demotes to probation. The scan case now resolves correctly: a scan key reaches the window, is evicted from it with estimated frequency 1, loses the comparison against a hot probation entry, and is discarded without ever displacing the working set.

Two refinements matter for adversarial and shifting workloads. The window/main ratio is **adaptive**: Caffeine hill-climbs the split, sampling the hit rate as the window grows or shrinks and following the gradient, so a recency-biased workload gets a larger window and a frequency-biased one a smaller one. And because CMS error is one-sided upward, a colliding or adversarially chosen key can appear permanently hot and block all candidates; Caffeine therefore admits **a small random fraction of rejected candidates** — those whose own estimate is at or above an internal threshold — which bounds how long a single inflated counter can hold the main space closed.

Caffeine's Efficiency wiki reports simulations over Wikipedia, database, search and OLTP traces in which W-TinyLFU reaches a near-optimal hit rate, improves substantially on LRU, and is competitive with Adaptive Replacement Cache (ARC) and Low Inter-reference Recency Set (LIRS) — while, unlike those two, **keeping no per-key ghost entries for evicted keys**: the fixed-size sketch takes the place of a ghost list.

### Implementation sketch (Scala)

The admission comparison, reduced to its load-bearing parts. `FrequencySketch` here is a 4-bit sketch packed into an `Array[Long]`, sixteen counters per word, one word per admissible entry.

```scala
final class FrequencySketch(maxSize: Int):
  // one 64-bit word per admissible entry: table length is maxSize rounded up to a power of two
  private val table = new Array[Long](Integer.highestOneBit((maxSize - 1).max(1)) * 2)
  private val sampleSize = 10 * maxSize
  private var size = 0

  private def indexOf(h: Int, i: Int): Int =
    val hash = h * 0x9e3779b1 + i * 0x7f4a7c15
    (hash ^ (hash >>> 15)) & (table.length - 1)

  /** Minimum of the four addressed 4-bit counters: never under-reports. */
  def frequency(key: Int): Int =
    (0 until 4).map { i =>
      val j = indexOf(key, i)
      ((table(j) >>> ((key + i) & 15) * 4) & 0xf).toInt
    }.min

  def increment(key: Int): Unit =
    for i <- 0 until 4 do
      val j = indexOf(key, i)
      val shift = ((key + i) & 15) * 4
      if ((table(j) >>> shift) & 0xf) < 15 then      // saturate at 15
        table(j) += 1L << shift
    size += 1
    if size >= sampleSize then reset()

  /** Halve every counter: bounds N, so the CMS error term stops growing. */
  private def reset(): Unit =
    var i = 0
    while i < table.length do
      table(i) = (table(i) >>> 1) & 0x7777777777777777L
      i += 1
    size /= 2

def admit(candidate: Int, victim: Int, sk: FrequencySketch): Boolean =
  sk.frequency(candidate) > sk.frequency(victim)
```

## Constructing a loading cache

```scala
//> using dep com.github.ben-manes.caffeine:caffeine:3.2.4
```

A `LoadingCache` computes missing values through a `CacheLoader`. Because `CacheLoader` is a single-abstract-method (SAM) interface, Scala 3 accepts a lambda:

```scala
import com.github.benmanes.caffeine.cache.{Caffeine, LoadingCache}
import java.time.Duration

val users: LoadingCache[UserId, User] =
  Caffeine.newBuilder[UserId, User]()          // explicit type arguments: Scala infers Object otherwise
    .maximumSize(10_000)                       // W-TinyLFU governs eviction
    .expireAfterWrite(Duration.ofMinutes(5))
    .recordStats()
    .build(id => db.loadUser(id))

val u: User = users.get(userId)
```

`get` on a loading cache does not return null: a miss invokes the loader, and **concurrent misses on the same key collapse onto a single load**, so a hot key expiring under load produces one upstream request rather than one per caller.

## Expiration versus refresh

The two duration knobs have different failure characteristics.

- **`expireAfterWrite` / `expireAfterAccess`** — invalidation. Past the deadline the entry is absent, so the next `get` blocks for the full load latency. Every reader of an expiring hot key pays a latency cliff at the moment of expiry.
- **`refreshAfterWrite`** — the next access *after* the interval schedules an **asynchronous** reload while the current value continues to be served. The reload happens off the caller's path; the served value is bounded-stale by the refresh interval plus load time.

They compose, and a common configuration refreshes frequently while expiring on a longer horizon so that a key which stops being requested eventually leaves the resident set:

```scala
val prices: LoadingCache[Sku, Price] =
  Caffeine.newBuilder[Sku, Price]()
    .maximumSize(50_000)
    .refreshAfterWrite(Duration.ofSeconds(30))
    .expireAfterWrite(Duration.ofMinutes(10))
    .recordStats()
    .build(sku => priceService.fetch(sku))
```

`expireAfter(Expiry)` supplies per-entry variable time-to-live (TTL), for instance honouring an upstream `Cache-Control: max-age` from RFC 9111. `weakKeys`, `weakValues` and `softValues` delegate reclamation to the garbage collector; entries then disappear on a schedule the cache does not control.

## Asynchronous loading

`AsyncLoadingCache` stores `CompletableFuture` values, so an in-flight miss is itself the cache entry and is shared rather than duplicated:

```scala
import com.github.benmanes.caffeine.cache.AsyncLoadingCache

val usersAsync: AsyncLoadingCache[UserId, User] =
  Caffeine.newBuilder[UserId, User]()
    .maximumSize(10_000)
    .expireAfterWrite(Duration.ofMinutes(5))
    .buildAsync((id, _) => db.loadUserAsync(id))   // (key, executor) => future
```

No carrier thread parks on the load, which matters under JEP 444 virtual threads only insofar as the loader itself is non-blocking; a blocking loader wrapped in a future merely relocates the block.

## Two-level caching in front of Redis

Caffeine is in-process: access is tens of nanoseconds, there is no network hop, and capacity is bounded by heap and replicated per node. Redis is out-of-process: one round-trip, shared across the fleet. Layering places Caffeine as level one (L1) and Redis as level two (L2), with the loader cascading:

```scala
import io.lettuce.core.api.sync.RedisCommands

val redis: RedisCommands[String, String] = connection.sync()

val profiles: LoadingCache[UserId, Profile] =
  Caffeine.newBuilder[UserId, Profile]()
    .maximumSize(20_000)
    .expireAfterWrite(Duration.ofMinutes(2))   // bounds cross-node staleness
    .recordStats()
    .build { id =>
      val key = s"profile:$id"
      Option(redis.get(key)).map(decode) match      // Lettuce returns null on miss
        case Some(p) => p
        case None =>
          val p = db.loadProfile(id)
          redis.setex(key, 300, encode(p))          // seconds, not millis
          p
    }
```

L1 absorbs the head of the Zipfian distribution and reduces L2 request rate by the L1 hit rate; L2 provides shared reach and survives a node restart. The design does not solve cross-node invalidation: **with k nodes each holding an independent L1, a write on one node leaves up to k − 1 stale copies for the remaining L1 TTL**, so the L1 TTL is the staleness bound unless invalidation is published explicitly, for example over Redis pub/sub.

## Instrumentation

```scala
val s = users.stats()
println(f"hit rate ${s.hitRate() * 100}%.1f%%  loads=${s.loadCount()}  evictions=${s.evictionCount()}")
```

A hit rate that does not rise as `maximumSize` rises indicates a workload with little reuse. The W-TinyLFU claim is that a given hit rate is reached at a **smaller** capacity than LRU requires, so tuning should follow this curve rather than a fixed heap budget.

## Pitfalls

- **A cache configured with `expireAfterWrite` alone shows periodic latency spikes at multiples of the TTL.** Every reader of a hot key blocks on reload the instant it expires; `refreshAfterWrite` at a shorter interval moves the reload off the read path.
- **`refreshAfterWrite` never fires on a key that stops being accessed.** Refresh is triggered by an access after the interval, not by a timer, so an idle entry stays at its original value until expiry or eviction removes it.
- **A `CacheLoader` that throws leaves no entry, and every caller retries against the failing upstream.** Caffeine does not cache failures, so an outage converts a cached key into full-rate traffic to the broken dependency.
- **`maximumWeight` and `maximumSize` cannot both be configured.** The two bounds are mutually exclusive: a builder that sets both throws `IllegalStateException` at `build` rather than silently preferring one, and a weigher that returns a value varying over an entry's lifetime corrupts the accounting because weight is captured at write.
- **Hit rate collapses when keys are effectively unique.** Request identifiers or timestamps as keys give every entry frequency 1, so the admission filter rejects candidates against equally cold victims and the cache degenerates to churn plus sketch overhead.
- **The frequency sketch survives eviction, so a key evicted and re-requested is admitted more readily than a genuinely new key.** This is intended, but it means a burst of a now-dead key set can suppress admission of a newly hot set until the next halving pass.
- **Distinct JVMs never agree.** L1 entries are per-process; a stale read on a node that missed an invalidation is invisible in that node's stats, which count only hits and misses, never incorrect hits.
