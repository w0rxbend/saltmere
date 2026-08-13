---
title: "Cache placement patterns: aside, read-through, write-through, write-behind"
date: 2026-08-13
track: sys-patterns
summary: "Where you put the read miss and the write determines your consistency and latency. Four patterns — cache-aside, read-through, write-through, write-behind — pick different points on that curve. Here's which fits which workload, and the dual-write traps to avoid."
reading_time: 6
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

A cache is a second copy of the truth, and every caching pattern is an answer to two questions: on a read miss, who fetches from the database — your code or the cache? And on a write, does the data go to the cache, the database, or both, and in what order? The four classic patterns are just the four useful combinations of those answers. (This is about *placement*; protecting a hot key against a thundering herd of concurrent misses is stampede/coalescing, covered separately.)

## The read side: cache-aside vs read-through

**Cache-aside** (lazy loading) keeps the cache dumb. Your application owns the miss.

```python
def get_user(uid):
    v = cache.get(f"user:{uid}")
    if v is not None:
        return v                      # hit
    row = db.query_user(uid)          # miss: app fetches
    cache.set(f"user:{uid}", row, ex=300)   # populate with TTL
    return row
```

Only requested keys are ever cached (memory-efficient), and a cache outage degrades to slow, not down. The costs: three round trips on a miss, and the population logic is duplicated at every call site.

**Read-through** pushes that logic *into* the cache: the app always asks the cache, and the cache library/provider fetches from the DB on a miss behind a loader function. Same population effect, centralized — but now the cache is on the critical write path of your data model, and cold starts hammer the backing store.

## The write side: write-through vs write-behind

**Write-through** writes the cache and the database synchronously on every update, keeping them coherent at the cost of write latency.

```python
def update_user(uid, row):
    db.write_user(uid, row)                 # 1. system of record
    cache.set(f"user:{uid}", row, ex=300)   # 2. keep cache warm
```

Reads after a write are always fresh; the penalty is that every write pays for two systems, and you cache data that may never be read again. Note the *ordering*: DB first, then cache. Do it the other way and a DB failure leaves the cache holding a value that was never committed.

**Write-behind** (write-back) acknowledges the write after hitting the cache, then flushes to the DB asynchronously — often batched or coalesced. This gives the lowest write latency and absorbs bursts, at the price of a durability window: a crash before flush loses acknowledged writes. Use it for high-volume, loss-tolerant data (counters, metrics, view logs), never for money.

## Trade-offs at a glance

| Pattern | Miss handled by | Write path | Read-after-write | Main risk |
| --- | --- | --- | --- | --- |
| Cache-aside | App | (pair with invalidate) | Stale until TTL/invalidate | Dual-write races |
| Read-through | Cache | — | Depends on write pattern | Cold-start stampede |
| Write-through | — | Sync DB + cache | Always fresh | Higher write latency |
| Write-behind | — | Async DB flush | Fresh in cache, lagged in DB | Data loss on crash |

## The invalidation traps

Most production caches are cache-aside for reads plus an explicit invalidation on write. The subtle bug is the **stale-set race** (documented in Facebook's memcache paper): reader A misses and fetches an old row; writer B updates the DB and deletes the key; reader A then sets the key with its stale value — and it sticks until TTL. Mitigations that actually work:

- **Delete, don't update, on write.** Let the next read repopulate. An update re-introduces ordering races; a delete is idempotent.
- **Bound every entry with a TTL** as a backstop, so any missed invalidation self-heals. Treat TTL as a correctness floor, not just eviction.
- **Version or lease keys.** Store a monotonic version alongside the value and refuse to overwrite a newer one — the memcache "leases" idea — which closes the stale-set window.

And the eternal reminder: a cache and a database are two systems with no shared transaction. "Write both" is a distributed dual write; you get atomicity only by making one the source of truth and the other a derivable, TTL-bounded copy.

**Try next:** Add a `version` field to your hottest cache-aside key and make writes do `DEL` (not `SET`); measure stale-read rate before and after under concurrent load.
