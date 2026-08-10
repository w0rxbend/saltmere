---
title: "Cache Read/Write Strategies: Cache-Aside, Read-Through, Write-Through, Write-Behind, Write-Around, Refresh-Ahead"
date: 2026-08-10
track: microservices
summary: "Six caching patterns, defined precisely with their read path and write path, the consistency/latency/complexity trade-offs, the failure modes that bite in production, and a decision rule for picking each. With cache-aside and write-through code."
reading_time: 7
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

"Add a cache" is not one decision. It's two: how a read populates the cache, and how a write keeps it honest. Get the pairing wrong and you ship stale prices, double-write bugs, or a cache that silently eats acknowledged writes when a node dies. The vocabulary below — cache-aside, read-through, write-through, write-behind, write-around, refresh-ahead — is exactly what interviewers probe, because each name pins down a specific read path *and* a specific write path with specific failure modes. Here they are, precisely.

The core distinction: in **cache-aside** your *application* orchestrates the cache. In **read-through / write-through / write-behind** the *cache itself* sits inline and talks to the database on your behalf (via a loader/writer, what Hazelcast calls a `MapStore` and Coherence a `CacheStore`). That single difference drives most of the trade-offs.

## The read-population patterns

**Cache-aside (lazy loading).** The application checks the cache; on a hit it returns, on a miss it reads the database, writes the value back into the cache, and returns it. The cache is a dumb key-value store that knows nothing about your database. AWS's whitepaper calls this lazy loading and names its two properties: only requested data is ever cached (lean, cheap), and a node failure is survivable — you just fall through to the database with higher latency.

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

Two gotchas live in that tiny function. First, the **cache-miss penalty**: a miss costs three round trips (cache, database, cache) instead of one, so the first request after expiry is the slowest. Second, **staleness**: the cached copy only refreshes on a miss, so if the database changes underneath you, readers see the old value until the TTL expires. The TTL *is* your consistency knob — it bounds staleness without guaranteeing freshness. When a hot key expires and thousands of readers miss at once, cache-aside degrades into a [cache stampede](/articles/microservices/2026-08-10-cache-stampede-request-coalescing); handle that separately.

**Read-through** is cache-aside's logic moved *inside* the cache. The app always asks the cache; on a miss the cache's loader fetches from the database, stores, and returns — transparently. Same read path, same three-trip penalty on a miss, same staleness profile. What you buy is a single choke point for load logic (no duplicated miss-handling scattered across services) and a natural home for stampede protection. What you pay is a cache product that supports loaders and code that runs where the cache lives. Coherence and Hazelcast implement exactly this.

**Refresh-ahead** attacks the miss penalty. The cache proactively reloads entries that are *about to* expire and are being actively read, so a popular key is refreshed in the background before anyone hits a miss. Coherence gates this on a fraction of the entry's expiry time: read an entry inside that window and it triggers an async reload. The win is latency — hot keys are effectively never cold. The costs: it only helps keys with predictable, repeated access (guess wrong and you waste database work refreshing entries nobody reads), and it's still eventually consistent within the refresh interval.

## The write patterns

The read strategy decides where a value *comes from*; the write strategy decides how the store and cache stay in agreement when data *changes*.

**Write-through.** Every write goes through the cache, which synchronously writes to the database before acknowledging. Cache and database are updated in lockstep, so the cache is never stale (Coherence and AWS both make this the headline benefit). The price is write latency — every write pays for two hops — and cache churn: you cache data on write that may never be read. AWS's practical advice is to *pair write-through with lazy loading and a TTL*, so writes keep hot data fresh while the TTL evicts the cold data write-through would otherwise pile up.

```python
def update_price(pid: int, price: float) -> None:
    key = f"product:{pid}"
    # write-through: DB first, then cache, both synchronously
    db.execute("UPDATE products SET price=%s WHERE id=%s", price, pid)
    r.set(key, json.dumps({"id": pid, "price": price}), ex=TTL)
```

The subtle failure mode is the **double-write race**: two independent statements with no shared transaction. If the database write commits and the process dies before the `r.set`, the cache holds the old price until TTL — a write-through cache that's briefly stale, exactly what it promised not to be. Worse is the read-modify-write interleave: two concurrent updates can commit to the database in one order and land in the cache in the other, leaving the cache permanently wrong. A true write-through cache with an inline `CacheStore` closes this by making the store write part of the cache operation; hand-rolled two-statement code does not. This is why "just delete the key on write" (invalidate rather than update) is often safer — a miss reloads truth, a bad update persists a lie.

**Write-behind (write-back).** The cache acknowledges the write immediately and flushes to the database asynchronously, usually batched and coalesced. This gives the best write latency and can collapse many updates to the same key into one database write — excellent for write-heavy, high-throughput workloads like counters and metrics. The failure mode is the one every interviewer wants named: **data loss on crash.** Between the acknowledgement and the flush, the authoritative copy lives only in the cache; if the node dies, acknowledged writes vanish. You've traded durability for latency. Hazelcast notes the related hazard — a failed backend write surfaces *after* the app has moved on, so error handling is asynchronous and awkward. Use it only where a bounded window of loss is acceptable, and prefer implementations that replicate the write-behind queue.

**Write-around.** Writes go straight to the database and bypass the cache entirely; the cache is populated only later, by a read miss (cache-aside/read-through). This keeps write-once/read-rarely data from polluting the cache. The trade-off is a guaranteed miss on the first read after a write — good when recently written data is unlikely to be read soon (audit logs, ingest pipelines), bad for read-your-writes.

## Comparison

| Pattern | Read path | Write path | Consistency | Write latency | Failure mode / gotcha |
|---|---|---|---|---|---|
| Cache-aside | App: cache→DB on miss, backfill | App writes DB (often invalidates key) | Stale within TTL | n/a (app-managed) | Miss penalty; stampede on hot expiry; app owns logic |
| Read-through | Cache loader fills on miss | (pair with a write pattern) | Stale within TTL | n/a | Needs loader support; still misses cold |
| Refresh-ahead | Cache pre-reloads hot keys | (pair with a write pattern) | Eventually, within refresh window | n/a | Wasted work on bad prediction |
| Write-through | Served from fresh cache | Cache→DB synchronously | Strong (cache ≈ DB) | High (two hops) | Double-write race; cache churn |
| Write-behind | Served from fresh cache | Cache acks, DB flush async | Eventual (DB lags) | Lowest | **Data loss on crash**; async error handling |
| Write-around | Cache filled by later miss | App writes DB, bypass cache | Stale within TTL | Low | First read after write always misses |

## Picking one

Reach for **cache-aside** by default — it's the simplest, survives cache outages, and pairs with any datastore. Move miss logic into **read-through** when you have many services hitting the same cache and want one place for loading and stampede control. Add **refresh-ahead** only for a known set of hot keys where the miss latency actually hurts. On writes, choose **write-through** when reads must never be stale and you can absorb the write latency (catalog data, config); choose **write-behind** when write throughput dominates and a small loss window is tolerable (counters, telemetry); choose **write-around** for data that's written far more often than it's read. In practice most production systems run **cache-aside + write-through-by-invalidation + TTL** — and treat the TTL as the real, honest bound on how stale a reader can ever be.

**Try next:** implement the cache-aside snippet, then change `update_price` to *delete* the key instead of `set`-ing it, and reason about which stale-read and double-write races each version still allows — then layer in [consistent hashing](/articles/distributed-systems/2026-07-25-consistent-hashing-ring) to see how these patterns behave across a multi-node cache.
