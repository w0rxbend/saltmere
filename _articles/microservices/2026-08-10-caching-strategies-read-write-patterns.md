---
title: "Cache Read/Write Strategies: Cache-Aside, Read-Through, Write-Through, Write-Behind, Write-Around, Refresh-Ahead"
date: 2026-08-10
track: microservices
summary: "Six caching patterns, defined by their read path and write path, with the consistency, latency and complexity trade-offs, the failure modes they exhibit in production, and a decision rule for each. Includes cache-aside and write-through code."
reading_time: 8
tags:
  - caching
  - redis
  - consistency
  - system-design
  - python
sources:
  - title: "AWS — Database Caching Strategies Using Redis: Caching Patterns"
    url: "https://docs.aws.amazon.com/whitepapers/latest/database-caching-strategies-using-redis/caching-patterns.html"
  - title: "Amazon ElastiCache — Caching strategies (lazy loading vs write-through, TTL)"
    url: "https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/Strategies.html"
  - title: "Oracle Coherence — Read-Through, Write-Through, Write-Behind, and Refresh-Ahead Caching"
    url: "https://docs.oracle.com/cd/E16459_01/coh.350/e14510/readthrough.htm"
  - title: "Hazelcast — Cache Access Patterns"
    url: "https://hazelcast.com/foundations/caching/cache-access-patterns/"
---

**Gist.** Introducing a cache splits into two independent decisions — how a read populates the cache, and how a write keeps cache and database in agreement — and the six named patterns (cache-aside, read-through, refresh-ahead, write-through, write-behind, write-around) each fix one half of that pairing. The mechanism common to all of them is a second copy of the data with its own update path, which buys read latency by removing a database round trip. The cost is that the second copy can disagree with the authoritative one: the patterns differ only in how long the disagreement lasts, who is responsible for ending it, and whether an acknowledged write can be lost entirely.

The structural distinction runs through every trade-off below. Under **cache-aside the application orchestrates the cache**, and the cache is a key-value store that knows nothing about the database. Under **read-through, write-through and write-behind the cache sits inline** and reaches the database itself through a loader/writer component — a `MapStore` in Hazelcast, a `CacheStore` in Coherence.

## Read-population patterns

**Cache-aside (lazy loading).** The application reads the cache; on a hit it returns the cached value, on a miss it reads the database, writes the value back into the cache, and returns it. The AWS whitepaper names this pattern lazy loading and records two properties: **only requested data is ever cached**, and **a cache node failure is survivable** — requests fall through to the database at higher latency rather than failing.

```python
import redis, json
r = redis.Redis()
TTL = 300  # seconds — the staleness ceiling

def get_product(pid: int) -> dict:
    key = f"product:{pid}"
    cached = r.get(key)
    if cached is not None:
        return json.loads(cached)          # hit
    row = db.query_one("SELECT * FROM products WHERE id=%s", pid)  # miss
    r.set(key, json.dumps(row), ex=TTL)    # populate, with TTL
    return row
```

Two costs are visible in that function. The **cache-miss penalty**: a miss costs three round trips (cache read, database read, cache write) where a hit costs one, so the first request after an entry expires is the slowest request in the workload. And **staleness**: the cached copy is refreshed only by a miss, so a database change made elsewhere is invisible to readers until the entry expires. **The time-to-live (TTL) is therefore the consistency knob — it bounds staleness without providing freshness.** When a hot key expires and many readers miss simultaneously, cache-aside degrades into a [cache stampede](/articles/microservices/2026-08-10-cache-stampede-request-coalescing), which requires separate handling.

**Read-through** relocates the same logic inside the cache. The application always addresses the cache; on a miss the cache's loader fetches from the database, stores the entry, and returns it. The read path, the three-trip miss penalty and the staleness profile are unchanged. What differs is placement: **miss handling exists once**, rather than being duplicated in every service, which also gives stampede protection a single home. The requirement is a cache product supporting loaders and the ability to run loader code where the cache runs. Coherence and Hazelcast both implement this pattern.

**Refresh-ahead** targets the miss penalty. The cache reloads entries that are close to expiry and are being actively read, so a frequently read key is refreshed in the background before any request observes a miss. Coherence gates the behaviour on a refresh-ahead factor expressed as a fraction of the entry's expiry time: a read that falls inside that window triggers an asynchronous reload. The benefit is latency on hot keys. The costs are that **the pattern helps only keys with repeated, predictable access** — reloading entries nobody subsequently reads spends database work for nothing — and that the value remains eventually consistent within the refresh interval.

## Write patterns

The read strategy determines where a value comes from; the write strategy determines how cache and database are reconciled when the value changes.

**Write-through.** Every write passes through the cache, which writes synchronously to the database before acknowledging. Cache and database advance in lockstep, so the cached entry is not stale — the benefit both Coherence and AWS state. The costs are **write latency, since each write pays two hops**, and cache churn, because data is cached at write time whether or not it is ever read. AWS recommends pairing write-through with lazy loading and a TTL, so that writes keep hot data fresh while the TTL evicts the cold entries write-through would otherwise accumulate.

```python
def update_price(pid: int, price: float) -> None:
    key = f"product:{pid}"
    # application-side write-through: DB then cache, synchronously, no shared transaction
    db.execute("UPDATE products SET price=%s WHERE id=%s", price, pid)
    r.set(key, json.dumps({"id": pid, "price": price}), ex=TTL)
```

The failure mode is the **double-write race**: the two statements share no transaction. If the database write commits and the process dies before `r.set`, the cache serves the previous price until the TTL expires — a write-through cache that is stale, contradicting the property it was chosen for. The more damaging variant is the read-modify-write interleave: **two concurrent updates can commit to the database in one order and reach the cache in the opposite order, leaving the cache permanently wrong** rather than briefly wrong, because nothing subsequently corrects it. An inline `CacheStore` narrows this by making the database write part of the cache operation on the key; hand-written two-statement code does not. This is the argument for invalidating on write rather than updating on write: a subsequent miss reloads the authoritative value, whereas an out-of-order update persists an incorrect one.

**Write-behind (write-back).** The cache acknowledges the write immediately and flushes to the database asynchronously, typically batched and coalesced so that repeated updates to one key collapse into a single database write. Write latency is the lowest of the patterns and database write volume falls, which suits counters and metrics. The failure mode is **loss of acknowledged writes on crash**: between acknowledgement and flush the only copy of the update lives in the cache, so a node failure destroys writes the application was told had succeeded. Durability has been exchanged for latency. Hazelcast records a related consequence: **a failed backend write surfaces after the application has moved on**, so error handling is asynchronous and detached from the request that caused it. The pattern is appropriate only where a loss window is acceptable; implementations that keep backup copies of the write-behind queue on other members reduce, but do not eliminate, the exposure.

**Write-around.** Writes go to the database and bypass the cache; the cache is populated later by a read miss under cache-aside or read-through. Write-once/read-rarely data therefore does not displace hot entries. The cost is a **guaranteed miss on the first read after a write**, which suits audit logs and ingest pipelines and conflicts directly with read-after-write expectations.

### Implementation sketch (Scala)

Write-behind's coalescing property — many updates to one key becoming one database write — reduces to a mutable map of pending values drained on a timer.

```scala
import scala.collection.mutable

final class WriteBehindBuffer[K, V](flush: Map[K, V] => Unit):
  private val pending = mutable.LinkedHashMap.empty[K, V]

  /** Acknowledges immediately; the value exists only here until drain(). */
  def put(key: K, value: V): Unit = synchronized:
    pending.update(key, value)   // overwrite collapses N updates into 1 write

  def drain(): Unit =
    val batch = synchronized:
      val snapshot = pending.toMap
      pending.clear()
      snapshot
    if batch.nonEmpty then
      try flush(batch)
      catch case e: Exception =>
        // the caller of put() has long since returned: no request to fail
        synchronized:
          batch.foreach((k, v) => pending.getOrElseUpdate(k, v))
```

Two properties are load-bearing. **`pending.update` is the coalescing step**: only the last value per key survives to the flush, so the database never observes the intermediate states. And **the window between `put` and `drain` is the loss window** — a crash inside it discards acknowledged writes, because `pending` is the only copy. The `getOrElseUpdate` in the recovery path restores failed entries without overwriting values written after the snapshot was taken.

## Comparison

| Pattern | Read path | Write path | Consistency | Write latency | Failure mode / gotcha |
|---|---|---|---|---|---|
| Cache-aside | App: cache→DB on miss, backfill | App writes DB (often invalidates key) | Stale within TTL | n/a (app-managed) | Miss penalty; stampede on hot expiry; app owns logic |
| Read-through | Cache loader fills on miss | (pair with a write pattern) | Stale within TTL | n/a | Needs loader support; still misses cold |
| Refresh-ahead | Cache pre-reloads hot keys | (pair with a write pattern) | Eventually, within refresh window | n/a | Wasted work on bad prediction |
| Write-through | Served from fresh cache | Cache→DB synchronously | Strong (cache ≈ DB) | High (two hops) | Double-write race; cache churn |
| Write-behind | Served from fresh cache | Cache acks, DB flush async | Eventual (DB lags) | Lowest | **Data loss on crash**; async error handling |
| Write-around | Cache filled by later miss | App writes DB, bypass cache | Stale within TTL | Low | First read after write always misses |

## Selection

**Cache-aside** is the default: it is the simplest, it survives cache outages, and it requires nothing of the datastore. Miss logic moves into **read-through** when many services share one cache and loading plus stampede control should exist in one place. **Refresh-ahead** applies to a known set of hot keys whose miss latency is measurably harmful. On the write side, **write-through** fits data that must not be read stale and can absorb the extra hop (catalogue entries, configuration); **write-behind** fits write-dominated workloads with a tolerable loss window (counters, telemetry); **write-around** fits data written far more often than read. The common production combination is **cache-aside with invalidation on write plus a TTL**, where the TTL is the honest bound on how stale any reader can be.

## Pitfalls

- **Updating the cache on write instead of invalidating it.** Symptom: a cached value that is wrong indefinitely rather than until the TTL. Cause: two concurrent writers commit to the database in one order and to the cache in the other, and nothing later corrects the losing value.
- **Treating write-through as atomic when it is two statements.** Symptom: a stale entry after a process restart. Cause: the database write committed and the process died before the cache write; without an inline `CacheStore` the pair has no shared transaction.
- **Relying on write-behind acknowledgements for durability.** Symptom: writes the application reported as successful are missing from the database after a node failure. Cause: between acknowledgement and flush the only copy of the update is in the cache.
- **Handling write-behind flush errors in the request path.** Symptom: backend write failures observed only in logs, with no client ever informed. Cause: the failure occurs after the originating request has returned.
- **Applying refresh-ahead to keys with unpredictable access.** Symptom: database load rises without a corresponding fall in read latency. Cause: entries are reloaded before expiry and then not read.
- **Combining write-around with read-after-write expectations.** Symptom: a client reads its own write and receives the previous value or a slow response. Cause: the write bypassed the cache, so the following read is a guaranteed miss.
- **Treating the TTL as a freshness guarantee.** Symptom: readers see data older than intended despite a short TTL. Cause: the TTL bounds how long a stale entry may persist; it does not cause any entry to be refreshed before it is requested.
