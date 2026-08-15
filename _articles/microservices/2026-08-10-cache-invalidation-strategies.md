---
title: "Cache Invalidation Strategies: From TTL to Change Data Capture"
date: 2026-08-10
track: microservices
summary: "A survey of cache invalidation mechanisms — absolute and sliding TTL, delete-on-write, versioned keys, write-through, event-driven purge via CDC or pub/sub, CDN surrogate keys, and leases — with the staleness-versus-load trade-off each imposes and the race that makes deletion alone unsafe."
reading_time: 7
tags:
  - caching
  - invalidation
  - redis
  - cdn
  - cdc
  - microservices
sources:
  - title: "Working with surrogate keys | Fastly Documentation"
    url: "https://www.fastly.com/documentation/guides/full-site-delivery/purging/working-with-surrogate-keys/"
  - title: "Client-side caching reference | Redis Docs"
    url: "https://redis.io/docs/latest/develop/reference/client-side-caching/"
  - title: "Cache Consistency: Strategies to Keep Data Fresh | Redis Blog"
    url: "https://redis.io/blog/cache-consistency-strategies/"
  - title: "Debezium connector for PostgreSQL (reference docs)"
    url: "https://debezium.io/documentation/reference/stable/connectors/postgresql.html"
  - title: "Scaling Memcache at Facebook — leases FAQ (MIT 6.824)"
    url: "https://pdos.csail.mit.edu/6.824/papers/memcache-faq.txt"
---

**Gist.** A cache holds a copy of data whose source of truth can change underneath it, so every caching system needs a rule for retiring copies. The available rules form a spectrum — expire on a timer, delete on write, retire a whole namespace by bumping a version, react to the database's own change log, or serialize the fill with a lease — and each moves the same dial in a different direction. **The cost is uniform in shape: the more precisely a mechanism retires stale entries, the more coupling, coordination or write-path latency it introduces.**

Two adjacent problems are treated elsewhere: keeping cache and database consistent under a **dual write**, and the **cache stampede** that follows the expiry of a hot key, covered at [/articles/microservices/2026-08-10-cache-stampede-request-coalescing](/articles/microservices/2026-08-10-cache-stampede-request-coalescing). What follows concerns the act of invalidation itself.

## Time-to-live: absolute and sliding

The cheapest mechanism performs no invalidation at all. An entry carries a time-to-live (TTL) and expires unattended. **Absolute TTL** fixes the lifetime at the moment of write: `SET key value EX 300` retires the entry 300 seconds later regardless of read traffic. **Sliding TTL** resets the timer on each access, so frequently-read entries persist and cold entries age out. Sliding suits session state; it is a poor fit for data with an external source of truth, because a continuously-read key can outlive the underlying record indefinitely.

The trade-off is structural. As the Redis team states it, "a cache relying on TTL alone serves stale data for the entire remaining window after the database changes." **With a five-minute TTL and a write ten seconds after the fill, readers observe the superseded value for the remaining four minutes and fifty seconds.** Shortening the TTL reduces the staleness bound and raises the miss rate, and therefore backend load. No setting improves both, which is why TTL usually serves as a *backstop* beneath a more precise mechanism rather than as the strategy.

Expiry timing carries its own hazard: **entries written together with an identical TTL expire together.** Ten thousand keys filled in one batch produce a single second of synchronized misses. Adding **jitter**, a random spread applied per entry, desynchronizes the expirations.

The same jitter applies to **negative caching** — caching the absence of a record to absorb repeated lookups for a missing key. Short, jittered TTLs prevent a wave of misses on the same absent key from refreshing in lockstep.

## Delete-on-write and the stale-set race

Explicit invalidation deletes the cache key when the database is written, so the next read repopulates it. This is the cache-aside write path. Its failure mode is a race between a concurrent reader and the writer:

1. The reader misses in the cache and reads the *superseded* value from the database.
2. The writer commits the new value and deletes the cache key.
3. The reader, still holding the superseded value, installs it into the cache.

**The cache now holds a stale entry with a full TTL ahead of it, and no subsequent write repairs it before expiry.** The Redis blog names this a **stale set**: "concurrent updates get reordered, leaving the cache holding a value that doesn't reflect the latest write." The racing window is narrow, but at large query and cache-fill volumes a rare interleaving occurs often enough to matter.

The usual mitigation is the **delayed double delete**: the writer deletes the key, commits, then schedules a second delete after a short interval intended to cover the slow reader's write-back. It does not make the sequence atomic; **it removes only those stale entries installed before the second delete fires, so a reader slower than the chosen delay still leaves a stale entry standing for a full TTL.** Correctness under this race requires leases or versioning instead.

## Write-through and write-behind

**Write-through** updates cache and database together on the write path, so the cache never trails. Redis guidance recommends it for "data where correctness is the point, like account balances and payments," and observes that "users tend to handle write latency better than read latency, so the double-write cost lands in the right place." Every write then waits on two systems, and a partial failure leaves an inconsistency requiring reconciliation. **Write-behind** (write-back) buffers the write and flushes asynchronously, trading durability for latency: a crash before the flush loses the buffered writes, which suits counters and metrics rather than monetary records.

## Versioned and generational keys

Retiring an entire *namespace* — every cached fragment for a product, every rendered page for a user — without scanning or deleting per entry requires indirection. A **version pointer** supplies it: the version number is embedded in the cache key, and the current version lives in one small cell. **Incrementing the version orphans every key of the previous generation in a single atomic operation**, with no scan and no delete storm. Orphaned entries are never requested again and disappear on their own TTL.

Versioning also removes the stale-set race. A slow reader that writes back late writes to the *old* version's key, which no subsequent read will construct. The cost is memory: dead generations occupy space until they expire, which is why versioned keys require a TTL backstop. The same pattern retires entries whose serialized form changed across a rolling deploy — one bump of a global schema version discards every entry written by the previous build.

### Implementation sketch (Scala)

```scala
// Generational keys over a minimal cache interface; TTL carries jitter so a
// batch of fills does not expire in lockstep.
trait Cache:
  def get(key: String): Option[String]
  def set(key: String, value: String, ttl: FiniteDuration): Unit
  def incr(key: String): Long

final class VersionedStore(cache: Cache, render: Long => String):

  private val rng = new scala.util.Random

  private def jittered(base: FiniteDuration, fraction: Double = 0.15): FiniteDuration =
    val delta = (base.toSeconds * fraction).toLong
    (base.toSeconds + rng.between(-delta, delta + 1)).seconds

  private def version(productId: Long): Long =
    cache.get(s"product:$productId:ver").map(_.toLong).getOrElse(0L)

  private def key(productId: Long, v: Long): String =
    s"product:$productId:v$v:render"

  def read(productId: Long): String =
    val k = key(productId, version(productId))
    cache.get(k).getOrElse:
      val fresh = render(productId)
      // A concurrent bump makes this write land on a generation nobody reads.
      cache.set(k, fresh, jittered(1.hour))
      fresh

  def invalidate(productId: Long): Unit =
    cache.incr(s"product:$productId:ver")
```

## Event-driven invalidation: pub/sub and change data capture

Delete-on-write assumes the writing service owns every mutation. Where several services, a batch job or an operator can modify a table, the reliable signal is the database's own change log. **Change data capture (CDC)** converts committed writes into an event stream: Debezium tails the PostgreSQL write-ahead log (or the MySQL binlog) and emits a `before`/`after`/`op` envelope per row change. A consumer maps each event to an invalidation — in practice a version bump keyed by the row identifier — so the cache reacts to every write rather than only to locally-issued ones.

**Because CDC reads the log after commit, it cannot emit an invalidation for a transaction that rolled back** — the failure mode characteristic of naive dual writes.

For same-process or single-cluster caches, Redis ships server-assisted client-side caching. With `CLIENT TRACKING ON`, the server records which keys a connection has read and pushes an invalidation when any of them change, over RESP3 push messages or, under RESP2, via a `SUBSCRIBE __redis__:invalidate` channel on a redirected connection. Default tracking costs server memory proportional to the number of tracked keys and the number of clients holding them; `BCAST` mode instead invalidates on subscribed key *prefixes* and stores nothing per key, **trading precision for zero server-side tracking memory**. **When invalidations are redirected to a second connection, the protocol exhibits a read/invalidate race** — the invalidation for a key can arrive before the reply carrying its value — and its documented remedy — mark the entry "caching-in-progress" and discard the write-back if an invalidation arrived in the interim — is the local analogue of the double delete. The race does not arise when data and invalidation share one connection, because the message order is then known.

## Tag-based invalidation at the content delivery network

At the edge the set of affected URLs is not enumerable: one content change may touch a hundred rendered pages. Content delivery networks address this with **surrogate keys** (Fastly's term; "cache tags" elsewhere). The origin attaches a space-separated header such as `Surrogate-Key: veggie seasonal central-mexico` to a response, and the network indexes objects by those tags. One object may carry many keys and one key may tag many objects; per Fastly's documentation, a key purge request purges every object associated with that key, so a single API call retires an arbitrary set. Fastly's **soft purge** marks tagged objects stale rather than removing them, permitting revalidation in place of a miss storm. Surrogate keys are generational invalidation applied to HTTP, with the tag index held by the network.

## Leases: serializing the fill

The strongest primitive comes from *Scaling Memcache at Facebook*. On a miss the cache issues a **lease token**, and the client must present that token to install the value. Two properties follow. First, stale sets become impossible: if the key is invalidated while the client is reading the database, the lease is voided and memcache ignores the client's set because the lease it supplies is invalid, so an in-flight stale read cannot overwrite fresher data. Second, the herd is bounded: the server grants a lease to "only the first client that misses," and the remainder wait briefly and re-read while one fetcher fills. The second property is a coalescing mechanism and is treated in the stampede article.

## Choosing

The mechanism follows the staleness tolerance. Pure TTL where a bounded staleness window is acceptable; delete-on-write with a double delete for single-writer cache-aside; versioned keys where a namespace must be retired atomically or survive a rolling deploy; CDC or pub/sub where writers are plural or external; surrogate keys at the edge; leases where a stale set is unacceptable. These compose: a precise signal above, an event-driven refresh in the middle, and a jittered TTL beneath as the backstop that bounds staleness when the precise signal is lost.

## Pitfalls

- **Sliding TTL on data with an external source of truth.** A key read once per second never expires, so a database change made by another writer is never reflected.
- **A batch of fills sharing one absolute TTL.** All entries expire in the same second and the resulting simultaneous misses reach the backend as one burst; jitter on each TTL is what separates them.
- **Delete-on-write without versioning.** A reader that missed before the write installs the superseded value after the delete, and the cache then serves it for a full TTL.
- **Treating the delayed double delete as correctness.** A reader slower than the chosen delay installs its stale value after the second delete has already run, and the entry then survives for a full TTL.
- **Versioned keys without a TTL.** Orphaned generations are unreachable but still resident, so memory grows with the invalidation rate rather than with the working set.
- **Write-behind for data that must survive a crash.** Writes acknowledged from the buffer are lost if the process dies before the flush.
- **Dual-write invalidation from application code.** An invalidation issued before commit fires for transactions that later roll back; CDC reads the log after commit and does not.
- **`BCAST` client-side tracking on a broad prefix.** Every change under the prefix invalidates entries the client never read, which converts a precise signal into a miss stream.
- **Hard purge at the edge in place of soft purge.** Evicting tagged objects forces every subsequent request to the origin, whereas marking them stale allows revalidation to absorb the load.
