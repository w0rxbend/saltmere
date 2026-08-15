---
title: "Caching Strategies and Their Failure Modes: Pick Your Staleness"
date: 2026-08-15
track: microservices
summary: "Cache-aside, read-through, write-through, write-behind, and refresh-ahead differ in exactly one thing: who does the loading and when you pay for it. Here is a comparison table, the classic cache-aside write race that Facebook's memcache paper solves with leases, and the failure-mode vocabulary — penetration, avalanche, hot keys — interviewers expect you to have."
reading_time: 6
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

Phil Karlton's line — *"there are only two hard things in Computer Science: cache invalidation and naming things"* — survives because the first half keeps being true. Every caching strategy is really an answer to one question: **when the source of truth changes, how does the cache find out?** The five standard strategies give five different answers, and each buys its consistency with a different failure mode.

## The five strategies

**Cache-aside (lazy loading).** The application owns both sides: read the cache, on miss read the DB and populate the cache; on write, update the DB and *invalidate* (delete, don't update) the key. It caches only what is actually requested and survives cache outages — the AWS docs' "node failures aren't fatal" — at the price of a three-trip miss penalty and staleness between DB change and cache expiry.

**Read-through.** Same shape, but the cache itself (or its client library) loads from the DB on miss. The application sees one API. Behaviorally identical to cache-aside; operationally it moves the loader code — and the coordination of concurrent misses — into one place.

**Write-through.** Writes go to the cache, which synchronously writes to the DB before acking. Reads never see stale data for keys written this way, but you pay write latency for every write and you cache plenty of data nobody reads ("cache churn" in the ElastiCache docs). Almost always paired with read-through, and with a TTL so unread keys eventually leave.

**Write-behind (write-back).** Writes hit the cache and are flushed to the DB asynchronously, often batched. Spectacular write throughput and DB load-smoothing; the trade is durability — a cache node dying takes unflushed writes with it — plus the DB is now *behind* the cache, so anything else reading the DB directly (reports, CDC, another service) sees old data.

**Refresh-ahead.** The cache proactively re-fetches keys shortly before expiry, so hot keys never take a miss. Works only when you can predict what will be requested; mispredict and you generate DB load for keys nobody wanted.

## Comparison

| Strategy | Who loads | Write path | Staleness window | Main failure mode |
|---|---|---|---|---|
| Cache-aside | App, on miss | DB, then invalidate key | Until TTL / invalidation | Write race → stale forever; miss penalty |
| Read-through | Cache, on miss | Same as cache-aside | Until TTL / invalidation | Same, plus cache is now on the critical path |
| Write-through | Cache, on write | Sync: cache → DB | ~0 for written keys | Write latency; churn from unread keys |
| Write-behind | Cache, on write | Async flush to DB | DB is stale, not cache | Data loss on cache failure; DB readers see old data |
| Refresh-ahead | Cache, pre-expiry | n/a (read-side) | ~0 for predicted keys | Wasted DB load on mispredicted keys |

## The classic cache-aside write race

Cache-aside's staleness is usually bounded by TTL — except for one interleaving that makes it *unbounded*. Reader A misses and reads value `v1` from the DB. Writer B updates the DB to `v2` and deletes the cache key. Then A, running late, finally executes its `SET`, installing `v1`. The cache now holds stale data until the next write, potentially forever if that key is read-mostly.

This is exactly the race the *Scaling Memcache at Facebook* paper (NSDI 2013) fixes with **leases**: on a miss, memcache hands the client a token; a delete for that key invalidates outstanding tokens, so the late `SET` from a stale read is refused. The same paper is why the invalidation is a *delete* and not an update — concurrent updates racing to `SET` can also interleave into permanent staleness, whereas delete merely costs one extra miss. Ordering matters too: as the Azure Cache-Aside docs note, update the store *before* invalidating the cache, or a reader can slip in and reload the old value into the window you just created.

If you can't run leases, the honest mitigations are short TTLs on read-modify-write keys and versioned keys (`user:42:v{updated_at}`) that make stale entries unreachable instead of trying to delete them.

## TTLs: jitter, stale-while-revalidate, negative caching

TTL is the backstop for every invalidation bug — but a *uniform* TTL is a synchronization device. Deploy a warm-up that loads 100k keys with `TTL=300` and you have scheduled 100k simultaneous misses for five minutes later (that's a **cache avalanche**). Always jitter:

```python
import random, time, json

TTL, JITTER = 300, 0.1          # 10% spread

def get_user(cache, db, user_id):
    key = f"user:{user_id}"
    hit = cache.get(key)
    if hit == b"__NEG__":        # negative cache: known-missing row
        return None
    if hit is not None:
        return json.loads(hit)
    row = db.fetch_user(user_id)             # miss: go to source
    ttl = int(TTL * (1 + random.uniform(-JITTER, JITTER)))
    if row is None:
        cache.set(key, b"__NEG__", ex=60)    # short TTL for "not found"
    else:
        cache.set(key, json.dumps(row), ex=ttl)
    return row

def update_user(cache, db, user_id, fields):
    db.update_user(user_id, fields)          # 1. source of truth first
    cache.delete(f"user:{user_id}")          # 2. then invalidate (not SET)
```

Two refinements ride on top. **Stale-while-revalidate** serves the expired value immediately while one background fetch refreshes it — the corpus covers this and its cousins (single-flight, XFetch) in [cache stampede: coalescing, XFetch, and stale-while-revalidate](/articles/microservices/2026-08-10-cache-stampede-request-coalescing/). **Negative caching** — the `__NEG__` sentinel above — remembers that a key does *not* exist, with a short TTL so real rows appear quickly once created.

## Failure modes by name

- **Thundering herd / stampede:** one popular key expires, N concurrent misses hit the DB at once. Defenses (request coalescing, probabilistic early refresh, locks with stale serving) are in the [stampede article](/articles/microservices/2026-08-10-cache-stampede-request-coalescing/) — the strategy-level point is that read-through and refresh-ahead centralize the fix, while raw cache-aside makes every caller solve it.
- **Cache penetration:** requests for keys that exist in neither cache nor DB (bots enumerating IDs, deleted rows). Every request is a guaranteed DB hit. Fix with negative caching and, for hostile key spaces, a Bloom filter of valid IDs in front.
- **Cache avalanche:** mass simultaneous expiry — synchronized TTLs or a cache node restart — sends a wall of misses to the DB. Fix with TTL jitter, warm restarts/replicas, and DB-side rate limiting so the floor holds.
- **Hot keys:** one celebrity key exceeds what a single cache shard can serve. Facebook's paper handles this with replication of hot keys across pools; the app-level version is key splitting (`key#0..N`, pick one at random) or a tiny in-process L1 cache for the top-N keys.

The interview-ready summary: cache-aside is the default because it is resilient and caches only what's read; write-through buys read-your-writes at write-latency cost; write-behind buys write throughput at durability cost; and every one of them still needs jittered TTLs as the invalidation bug backstop.

**Try next:** reproduce the write race — two threads, one sleeping between DB read and cache `SET` — then fix it with a versioned key and confirm the stale `SET` becomes harmless.
