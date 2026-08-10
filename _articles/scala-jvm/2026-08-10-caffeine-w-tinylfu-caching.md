---
title: "Caffeine and W-TinyLFU: Near-Optimal In-Process Caching on the JVM"
date: 2026-08-10
track: scala-jvm
summary: "Caffeine is the caching library that replaced Guava Cache on the JVM. Here's why its W-TinyLFU eviction policy gets near-optimal hit rates, and concrete Scala/Java code for LoadingCache, AsyncLoadingCache, expireAfterWrite + refreshAfterWrite, and pairing it as an L1 in front of Redis."
reading_time: 6
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

Every backend eventually grows a cache. The interesting question is not *whether* to cache but *what to keep* when the cache is full and something has to go. That decision — the eviction policy — is where most in-process caches quietly leak hit rate. On the JVM, the current answer is **Caffeine** (latest release **3.2.4**), a library whose tagline is literally "a high performance, near optimal caching library." It is the acknowledged successor to Guava Cache, written by Ben Manes drawing on his work on both Guava's cache and the older `ConcurrentLinkedHashMap`. Spring, Quarkus, and countless services default to it. This article is about why it wins, and how to actually use it.

## The problem with LRU

Plain LRU (least-recently-used) evicts whatever you touched longest ago. It is cheap and it captures *recency*, which matters. But it is blind to *frequency*. Scan a large table once and LRU dutifully caches every row you touched, flushing out the handful of genuinely hot entries you hit thousands of times a minute. A single sequential scan can wipe a well-warmed cache — the classic "scan resistance" failure.

The academic fix is an admission policy: before letting a new item *into* the cache, ask whether it is actually more valuable than the item it would evict. That is what **TinyLFU** (Einziger, Friedman, Manes) formalizes, and what Caffeine implements as **Window TinyLFU (W-TinyLFU)**.

## How W-TinyLFU works

The clever part is estimating frequency without storing a counter per key forever. Caffeine keeps a **4-bit CountMinSketch** — a probabilistic frequency table that grows at roughly 8 bytes per cache entry, not per key ever seen. It answers "how often has this key been accessed recently?" approximately, in O(1), with a tiny footprint, and it periodically ages/halves its counters so old popularity decays.

Around that sketch, the cache is structured in two spaces:

- A small **admission window**, managed as an LRU. New entries land here first.
- A large **main space**, a **Segmented LRU** split into a *probation* and a *protected* region.

When an entry is evicted from the window, it does not go straight into the main space. Instead TinyLFU acts as a bouncer: it compares the estimated frequency of the *window's victim* against the estimated frequency of the *main space's victim* (the entry that would be evicted to make room) and keeps the one with the higher historic usage. As the Design wiki puts it, the policy chooses "to retain the entry with the highest historic usage." Recency (the window and the LRU ordering) and frequency (the sketch) are combined instead of one overriding the other.

Two refinements make it robust. The window/main split is **adaptive**: Caffeine uses hill-climbing to grow the window for recency-biased workloads and shrink it for frequency-biased ones. And to stop a hash collision or an adversary from inflating a victim's apparent frequency, it adds jitter — randomly admitting about 1% of rejected candidates that have a moderate frequency.

The payoff is measured, not asserted. Caffeine's Efficiency wiki publishes simulations across Wikipedia, database, search, and OLTP traces showing W-TinyLFU delivering "a near optimal hit rate," "a substantial improvement to LRU," and being "competitive with ARC and LIRS" — while, unlike those, not retaining evicted keys.

## Building a LoadingCache

Enough theory. Add the dependency:

```scala
//> using dep com.github.ben-manes.caffeine:caffeine:3.2.4
```

A `LoadingCache` computes and stores missing values through a `CacheLoader`. Because `CacheLoader` is a functional interface, Scala 3 lets you pass a lambda directly:

```scala
import com.github.benmanes.caffeine.cache.{Caffeine, LoadingCache}
import java.time.Duration

val users: LoadingCache[UserId, User] =
  Caffeine.newBuilder()
    .maximumSize(10_000)                       // size-based eviction (W-TinyLFU)
    .expireAfterWrite(Duration.ofMinutes(5))   // time-based expiration
    .recordStats()                             // enable hit/miss statistics
    .build(id => db.loadUser(id))              // CacheLoader as a SAM

val u: User = users.get(userId)  // loads on miss, returns cached on hit
```

`get` never returns null for a loading cache: a miss triggers the loader, and concurrent requests for the same key collapse onto a single load. `maximumSize` is where the whole eviction machine above kicks in — Caffeine, not you, decides what to drop.

## Expiration, refresh, and the difference between them

Two knobs people constantly confuse:

- **`expireAfterWrite` / `expireAfterAccess`** — *invalidation*. Once the duration passes, the entry is gone; the next `get` blocks and reloads.
- **`refreshAfterWrite`** — *freshness without a latency cliff*. After the interval, the *next access* triggers an **asynchronous** reload while continuing to serve the existing (slightly stale) value. Nobody waits.

They compose. A common pattern: refresh often so data stays fresh, but expire on a longer horizon so a value that stops being requested eventually leaves:

```scala
val prices: LoadingCache[Sku, Price] =
  Caffeine.newBuilder()
    .maximumSize(50_000)
    .refreshAfterWrite(Duration.ofSeconds(30))  // async reload after 30s on access
    .expireAfterWrite(Duration.ofMinutes(10))   // hard drop after 10m
    .recordStats()
    .build(sku => priceService.fetch(sku))
```

There is also `expireAfter(Expiry)` for per-entry, variable TTLs (e.g. honor an upstream `Cache-Control` max-age), plus `weakKeys`, `weakValues`, and `softValues` for reference-based eviction when you want the GC to reclaim entries under memory pressure.

## Asynchronous loading

If your loader is itself non-blocking — a reactive DB driver, an async HTTP client — use an `AsyncLoadingCache`. Values are stored as `CompletableFuture`s, so an in-flight miss is shared rather than duplicated, and callers compose without blocking a thread:

```scala
import com.github.benmanes.caffeine.cache.AsyncLoadingCache
import java.util.concurrent.CompletableFuture

val usersAsync: AsyncLoadingCache[UserId, User] =
  Caffeine.newBuilder()
    .maximumSize(10_000)
    .expireAfterWrite(Duration.ofMinutes(5))
    // (key, executor) => CompletableFuture[User]  -- the async loader form
    .buildAsync((id, _) => db.loadUserAsync(id))

val f: CompletableFuture[User] = usersAsync.get(userId)
```

This pairs naturally with virtual threads or a `Future`-based stack; nothing parks a carrier thread waiting on I/O.

## Reading the stats

`recordStats()` is cheap and worth always turning on in services. It tells you whether the cache is earning its memory:

```scala
val s = users.stats()
println(f"hit rate ${s.hitRate() * 100}%.1f%%  loads=${s.loadCount()}  evictions=${s.evictionCount()}")
```

A hit rate that is low *and not improving as you raise `maximumSize`* usually means the workload has little reuse — cache less, not more. W-TinyLFU's whole value proposition is that you reach a high hit rate at a *smaller* size than LRU would need, so watch this number when you tune capacity.

## Caffeine as L1 in front of Redis (L2)

Caffeine is in-process: nanosecond access, zero network, but per-node and bounded by heap. Redis is out-of-process: shared across your fleet, larger, but a network round-trip away. The standard move is to layer them — Caffeine as **L1**, Redis as **L2** — and let the loader cascade:

```scala
val profiles: LoadingCache[UserId, Profile] =
  Caffeine.newBuilder()
    .maximumSize(20_000)
    .expireAfterWrite(Duration.ofMinutes(2))   // short L1 TTL bounds staleness
    .recordStats()
    .build { id =>
      redis.get(s"profile:$id")                // L2 hit?
        .getOrElse {
          val p = db.loadProfile(id)           // L3: source of truth
          redis.setex(s"profile:$id", 300, p)  // populate L2
          p
        }
    }
```

L1 absorbs the hottest keys and shields Redis from the bulk of traffic; L2 gives shared reach and survives a node restart. The one thing this design does *not* solve is cross-node invalidation — because each JVM has its own L1, a write on node A leaves node B's copy stale until its short TTL expires. Keep L1 TTLs modest, or publish invalidation events over Redis pub/sub. That trade-off, and multi-level caching in general, is its own topic in this series.

## Why it's the default

Caffeine gives you Guava's familiar builder API with a materially better eviction algorithm underneath, an async story built on `CompletableFuture`, refresh-ahead to hide reload latency, and honest built-in metrics — all in O(1) with a few bytes of overhead per entry. For interviews, the one-line version: *Caffeine uses W-TinyLFU, a CountMinSketch frequency filter admitting into a segmented LRU, to combine recency and frequency and hit near-optimal hit rates where plain LRU can't.*

**Try next:** Build a `LoadingCache` with `recordStats()`, replay a workload that mixes a hot key set with a one-off scan, and print `stats().hitRate()`. Then swap in a naive `LinkedHashMap`-based LRU of the same size and compare — the scan-resistance gap is the whole point of W-TinyLFU.
