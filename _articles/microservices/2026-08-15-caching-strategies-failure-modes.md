---
title: "Caching Strategies and Their Failure Modes"
date: 2026-08-15
track: microservices
summary: "Cache-aside, read-through, write-through, write-behind, and refresh-ahead differ in one dimension: which component performs the load, and when the cost is paid. This article compares the five, walks the cache-aside write race that the Facebook memcache paper addresses with leases, and names the standard failure modes — penetration, avalanche, hot keys."
reading_time: 7
tags: [caching, cache-aside, write-through, ttl, invalidation, resilience]
sources:
  - title: "Caching strategies — Amazon ElastiCache Developer Guide"
    url: "https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/Strategies.html"
  - title: "Cache-Aside pattern — Azure Architecture Center"
    url: "https://learn.microsoft.com/en-us/azure/architecture/patterns/cache-aside"
  - title: "Nishtala et al. — Scaling Memcache at Facebook (NSDI 2013)"
    url: "https://www.usenix.org/system/files/conference/nsdi13/nsdi13-final170_update.pdf"
  - title: "Caching Strategies and How to Choose the Right One (CodeAhoy)"
    url: "https://codeahoy.com/2017/08/11/caching-strategies-and-how-to-choose-the-right-one/"
  - title: "Martin Fowler — TwoHardThings (the Phil Karlton quote)"
    url: "https://martinfowler.com/bliki/TwoHardThings.html"
---

**Gist.** A cache is a second copy of data whose source of truth changes independently, so every caching strategy is an answer to one question: **when the store changes, how does the copy find out?** The five standard strategies — cache-aside, read-through, write-through, write-behind, refresh-ahead — differ in which component performs the load and at which point in the request the cost falls. Each purchases its consistency with a distinct failure mode: an unbounded staleness window, added write latency, lost writes, or wasted load on the store.

Phil Karlton's line — *"there are only two hard things in Computer Science: cache invalidation and naming things"* — persists because the first half remains true.

## The five strategies

**Cache-aside (lazy loading).** The application owns both sides: read the cache; on a miss read the database and populate the cache; on a write, update the database and *invalidate* (delete, not update) the key. Only requested keys are ever cached, and a cache outage degrades rather than breaks the path — the ElastiCache guide's point that node failures are not fatal. The costs are a three-trip miss penalty and a staleness window running from the database change to the invalidation or expiry.

**Read-through.** The same shape, except the cache or its client library performs the load on a miss. The application sees a single API. Behaviourally this is cache-aside; operationally it relocates the loader — and the coordination of concurrent misses on the same key — into one component.

**Write-through.** Writes go to the cache, which writes synchronously to the database before acknowledging. Reads of keys written this way do not observe stale data, at the cost of database write latency on every write and of caching data nobody reads — the "cache churn" of the ElastiCache guide. It is normally paired with read-through and with a time-to-live (TTL) so unread keys eventually leave.

**Write-behind (write-back).** Writes land in the cache and are flushed to the database asynchronously, often batched, which smooths database load. The trade is durability — **a cache node lost before flush takes its unflushed writes with it** — plus an inversion of roles: the database is now behind the cache, so every other consumer reading the database directly (reporting, change data capture, another service) observes old data.

**Refresh-ahead.** The cache re-fetches keys shortly before expiry so that keys predicted to be hot never take a miss. It depends on the prediction being right; keys refreshed but not requested generate database load for nothing.

## Comparison

| Strategy | Who loads | Write path | Staleness window | Main failure mode |
|---|---|---|---|---|
| Cache-aside | App, on miss | DB, then invalidate key | Until TTL / invalidation | Write race → stale forever; miss penalty |
| Read-through | Cache, on miss | Same as cache-aside | Until TTL / invalidation | Same, plus cache is now on the critical path |
| Write-through | Cache, on write | Sync: cache → DB | ~0 for written keys | Write latency; churn from unread keys |
| Write-behind | Cache, on write | Async flush to DB | DB is stale, not cache | Data loss on cache failure; DB readers see old data |
| Refresh-ahead | Cache, pre-expiry | n/a (read-side) | ~0 for predicted keys | Wasted DB load on mispredicted keys |

## The cache-aside write race

Cache-aside staleness is normally bounded by the TTL. One interleaving makes it **unbounded**:

1. Reader A misses and reads value `v1` from the database.
2. Writer B updates the database to `v2` and deletes the cache key.
3. Reader A, delayed between its read and its write, executes its `SET`, installing `v1`.

The cache now holds `v1` with a fresh TTL and no pending invalidation. **The entry stays wrong until the next write to that key**, which for a read-mostly key may be indefinitely. The invariant the interleaving violates is that a populating `SET` must be derived from a database read that no invalidation has superseded.

*Scaling Memcache at Facebook* (NSDI 2013) enforces exactly that invariant with **leases**: a miss returns a token alongside the miss, a delete of the key invalidates outstanding tokens for it, and a `SET` presenting an invalidated token is refused. Reader A's late write is rejected, and the next reader takes a miss instead of a stale hit.

The same reasoning explains why the invalidation is a delete rather than an update: two writers racing to `SET` different values can interleave into permanent staleness in the same way, whereas a delete costs at most one extra miss. Ordering within the write path matters as well — the Azure Cache-Aside guidance updates the store *before* invalidating the cache. The reverse order opens a window between the invalidation and the commit in which a reader reloads the pre-update value and installs it with a full TTL.

Where leases are unavailable, two mitigations hold without new infrastructure: short TTLs on read-modify-write keys, which bound the damage, and **versioned keys** such as `user:42:v{updated_at}`, which make a stale entry unreachable rather than requiring it to be deleted — the writer changes the key name, and the old entry ages out on its own TTL.

## TTLs: jitter, stale-while-revalidate, negative caching

The TTL is the backstop for every invalidation bug, but a *uniform* TTL is a synchronisation device. A warm-up that loads 100k keys with `TTL=300` schedules 100k simultaneous misses five minutes later — a **cache avalanche**. Jitter breaks the correlation by spreading expiry over a window.

Two refinements sit on top. **Stale-while-revalidate** serves the expired value immediately while a single background fetch refreshes it; the corpus covers it and its relatives (single-flight, XFetch) in [cache stampede: coalescing, XFetch, and stale-while-revalidate](/articles/microservices/2026-08-10-cache-stampede-request-coalescing/). **Negative caching** stores a sentinel recording that a key does not exist, with a short TTL so that a row created later becomes visible quickly.

### Implementation sketch (Scala)

The read path combines jittered TTL, negative caching and delete-after-write. `Cache` and `Db` stand in for whatever clients are in use.

```scala
enum Entry:
  case Present(json: String)
  case Missing                       // negative cache sentinel

final class UserReads(cache: Cache, db: Db):
  private val Ttl        = 300
  private val Jitter     = 0.10      // ±10% spread breaks synchronised expiry
  private val MissingTtl = 60        // short: a created row must appear quickly

  private def jittered(base: Int): Int =
    val spread = base * Jitter
    (base + (scala.util.Random.between(-spread, spread))).toInt

  def get(userId: Long): Option[User] =
    val key = s"user:$userId"
    cache.get(key) match
      case Some(Entry.Missing)       => None
      case Some(Entry.Present(json)) => Some(User.fromJson(json))
      case None =>
        val row = db.fetchUser(userId)
        row match
          case None    => cache.set(key, Entry.Missing, MissingTtl)
          case Some(u) => cache.set(key, Entry.Present(u.toJson), jittered(Ttl))
        row

  def update(userId: Long, fields: Map[String, String]): Unit =
    db.updateUser(userId, fields)     // source of truth first
    cache.delete(s"user:$userId")     // then invalidate; never SET the new value
```

The `set` on the miss path is the statement exposed to the write race above; nothing in this sketch prevents it, which is what leases or versioned keys add.

## Failure modes by name

- **Thundering herd / stampede:** a popular key expires and N concurrent misses reach the database together. Defences — request coalescing, probabilistic early refresh, locks with stale serving — are in the [stampede article](/articles/microservices/2026-08-10-cache-stampede-request-coalescing/). At strategy level, read-through and refresh-ahead centralise the fix in one component; raw cache-aside leaves every caller to solve it independently.
- **Cache penetration:** requests for keys present in neither cache nor database (identifier enumeration, deleted rows). Each such request is a guaranteed database hit, because a miss is indistinguishable from a cold entry. Negative caching removes the repeat cost; for a hostile key space, a Bloom filter of valid identifiers rejects most requests before the lookup.
- **Cache avalanche:** mass simultaneous expiry — synchronised TTLs, or a cache node restart discarding its whole working set — sends a wall of misses at the database. Mitigations are TTL jitter, warm restarts or replicas, and database-side rate limiting so the store degrades rather than collapses.
- **Hot keys:** a single key's request rate exceeds what one cache shard can serve, so sharding by key does not help. The Facebook paper's answer is a *replication pool*: the item set is replicated across the pool's servers rather than sharded over them, so the request rate for one key is spread over several machines. The application-level equivalents are key splitting (`key#0..N` with a random choice per read) and a small in-process first-level cache for the top-N keys.

## Pitfalls

- Invalidating the cache before updating the database lets a concurrent reader reload the pre-update value into the gap; the entry is then stale with a full TTL ahead of it.
- Updating the cache with the new value instead of deleting it reintroduces the race between two writers: the `SET` that lands last wins regardless of which database write committed last.
- A uniform TTL applied during bulk warm-up produces synchronised expiry; the symptom is a periodic database load spike at exactly the TTL interval after deployment.
- Negative-cache entries given the same TTL as real entries delay the visibility of newly created rows by the full TTL, and the row appears absent despite existing in the database.
- Write-behind leaves the database behind the cache, so any consumer reading the database directly — reports, change data capture, a second service — observes values the cache has already superseded.
- A cache-aside `SET` whose database read happened before an intervening invalidation installs stale data with no pending invalidation to correct it; for a read-mostly key the entry can remain wrong until the next write.
- Refresh-ahead applied to keys that are not in fact requested converts a saved miss into recurring database load proportional to the number of mispredicted keys.
