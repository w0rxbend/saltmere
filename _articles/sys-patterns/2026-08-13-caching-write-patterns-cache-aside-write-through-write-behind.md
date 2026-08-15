---
title: "Cache placement patterns: aside, read-through, write-through, write-behind"
date: 2026-08-13
track: sys-patterns
summary: "The placement of the read miss and of the write fixes a system's consistency and latency. Cache-aside, read-through, write-through and write-behind occupy four different points on that curve; each admits a distinct dual-write failure mode."
reading_time: 7
tags: [caching, cache-aside, write-through, write-behind, consistency, redis, sys-patterns]
sources:
  - title: "Cache-Aside pattern — Azure Architecture Center (Microsoft Learn)"
    url: "https://learn.microsoft.com/en-us/azure/architecture/patterns/cache-aside"
  - title: "Cache consistency strategies to keep data fresh (Redis blog)"
    url: "https://redis.io/blog/cache-consistency-strategies/"
  - title: "Database Caching Strategies Using Redis (AWS Whitepaper)"
    url: "https://docs.aws.amazon.com/whitepapers/latest/database-caching-strategies-using-redis/welcome.html"
  - title: "Scaling Memcache at Facebook (NSDI 2013)"
    url: "https://www.usenix.org/system/files/conference/nsdi13/nsdi13-final170_update.pdf"
  - title: "Caching Strategies and How to Choose the Right One (CodeAhoy)"
    url: "https://codeahoy.com/2017/08/11/caching-strategies-and-how-to-choose-the-right-one/"
---

**Gist.** A cache is a second copy of the truth, and no transaction spans it and the database, so every write to both is a distributed dual write. The four classic placement patterns — cache-aside, read-through, write-through, write-behind — differ only in who handles a read miss and in the order and synchrony of the two writes. Each choice buys latency or coherence and pays for it in the other, and the pattern that acknowledges before the database commits pays additionally with a window in which acknowledged data is not yet durable.

Two questions determine the pattern. On a read miss, does the application fetch from the database, or does the cache do it? On a write, does the data reach the cache, the database, or both, and in what order? The four patterns are the four useful combinations. This article concerns *placement* only; protecting a single hot key against a thundering herd of concurrent misses is stampede protection and request coalescing, treated separately.

## The read side: cache-aside versus read-through

**Cache-aside** (lazy loading) leaves the cache passive: it stores and returns bytes and knows nothing about the system of record. The application owns the miss path — read the key, and on absence query the database, populate the key with a time-to-live (TTL), and return the row.

Two properties follow directly. **Only keys that some request has asked for are ever resident**, so cache memory tracks the working set rather than the whole table. And **a cache outage degrades the system to slow rather than down**, because the miss path is the ordinary database path. The costs are equally direct: a miss costs **three round trips** (cache read, database query, cache write) instead of one, and the population logic is duplicated at every call site that reads the entity.

**Read-through** relocates that logic inside the cache. The application always addresses the cache; the cache library or provider invokes a loader function on a miss and fetches from the database itself. The population effect is identical and centralised in one loader, but the cache now sits on the mandatory path for reads: it is no longer an optional accelerator, and a cold cache directs the full read load at the backing store until it fills.

## The write side: write-through versus write-behind

**Write-through** writes the database and the cache synchronously on every update. Because the acknowledgement waits for both, the interval in which the two disagree is bounded by the write call itself rather than by a TTL. Two costs follow: every write pays the latency of both systems, and entries are populated for data that may never be read again, consuming cache memory on a write-heavy, read-light key space.

The **ordering is load-bearing: the database write must commit first, then the cache is set**. Under the reverse order, a database failure leaves the cache serving a value that was never committed — the cache becomes the only holder of a state the system of record rejected, and it is served as truth until its TTL expires.

**Write-behind** (write-back) acknowledges the write once it has reached the cache, then flushes to the database asynchronously, commonly batching or coalescing repeated updates to the same key into a single database write. This yields the lowest write latency and absorbs bursts that the database could not accept at arrival rate. The price is a **durability window: writes acknowledged to the client but not yet flushed are lost if the cache process dies**. That makes the pattern appropriate for high-volume, loss-tolerant data — counters, metrics, view logs — and inappropriate for data whose loss is not recoverable by re-derivation, such as monetary transactions.

## Trade-offs at a glance

| Pattern | Miss handled by | Write path | Read-after-write | Main risk |
| --- | --- | --- | --- | --- |
| Cache-aside | App | (pair with invalidate) | Stale until TTL/invalidate | Dual-write races |
| Read-through | Cache | — | Depends on write pattern | Cold-start stampede |
| Write-through | — | Sync DB + cache | Always fresh | Higher write latency |
| Write-behind | — | Async DB flush | Fresh in cache, lagged in DB | Data loss on crash |

## The stale-set race

Most production caches combine cache-aside reads with an explicit invalidation on write. The characteristic defect of that combination is the **stale-set race**, documented in the Facebook memcache paper (NSDI 2013). The interleaving:

1. Reader A misses on key `k` and reads the old row from the database.
2. Writer B updates the row in the database and deletes `k` from the cache.
3. Reader A completes its populate and sets `k` to the value it read in step 1.

The delete in step 2 preceded the set in step 3, so **the cache ends holding a value older than the committed database row, and holds it until the TTL expires** — not for a request, but for the full remaining lifetime of the entry. The window is the duration of reader A's database round trip, which widens under load exactly when writes are most frequent.

Three mitigations, in increasing strength:

- **Delete rather than update on write.** A delete is idempotent and leaves the next reader to repopulate from the committed row. An update on the write path reintroduces the ordering problem in the opposite direction, since two concurrent writers may apply their sets in either order.
- **Bound every entry with a TTL** so that any missed or lost invalidation self-heals within a known interval. Under this pattern TTL is a correctness bound, not merely an eviction policy.
- **Version or lease the key.** The memcache paper's *leases* attach a token to the miss and reject a set whose token has been invalidated by an intervening write. A version counter stored beside the value achieves the same effect when the store offers a compare-and-set: a populate carrying an older version is refused rather than applied.

The underlying constraint is that a cache and a database are two systems with no shared transaction. Atomicity across them is unavailable; what remains is to designate one as the system of record and treat the other as a derivable, TTL-bounded copy whose divergence is bounded rather than prevented.

### Implementation sketch (Scala)

Cache-aside with a version guard. The populate carries the version observed before the database read and is discarded if a writer bumped the version in the interim, which closes the stale-set window in step 3 above.

```scala
final case class Versioned[A](version: Long, value: A)

trait VersionedCache[A]:
  def get(key: String): Option[Versioned[A]]
  def currentVersion(key: String): Long
  /** Applies only if the stored version is still `expected`; false otherwise. */
  def setIfVersion(key: String, expected: Long, entry: Versioned[A], ttl: Long): Boolean
  /** Bumps the version and removes any stored entry. */
  def bumpVersion(key: String): Long

def read[A](cache: VersionedCache[A], key: String, load: String => A): A =
  cache.get(key) match
    case Some(hit) => hit.value
    case None =>
      val seen = cache.currentVersion(key)   // observed before the database read
      val row  = load(key)
      // A concurrent write bumps the version; the populate is then dropped,
      // leaving a miss for the next reader rather than a stale hit.
      cache.setIfVersion(key, seen, Versioned(seen, row), ttl = 300)
      row

def write[A](cache: VersionedCache[A], key: String, store: (String, A) => Unit, row: A): Unit =
  store(key, row)          // system of record commits first
  cache.bumpVersion(key)   // invalidates in-flight populates and the current entry
```

## Pitfalls

- **Setting the cache before committing the database** leaves the cache serving a value the database rejected; the divergence lasts until the TTL, and reads during that interval are indistinguishable from correct hits.
- **Cache-aside without a TTL** turns any single lost invalidation — a dropped delete, a cache node restart with a stale peer, a code path that forgot the delete — into permanent staleness, because nothing else ever removes the entry.
- **Updating rather than deleting on write** makes the final value depend on the arrival order of two writers' sets, which is not the order in which their database transactions committed.
- **A read-through cold start** sends every read to the backing store at once; the store sees full read load with no warm-up, and the resulting latency is often mistaken for a database fault.
- **Write-behind on data that cannot be re-derived** loses writes that were already acknowledged to the client, so the client's view and the database disagree with no record of the discrepancy anywhere in the system.
- **Assuming the two writes are atomic.** No transaction spans the cache and the database, so any reasoning that treats "write both" as a single step is unsound regardless of how the two calls are ordered.
