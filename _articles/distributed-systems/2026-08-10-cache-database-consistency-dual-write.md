---
title: "Cache vs. database: the dual-write problem and how to actually solve it"
date: 2026-08-10
track: distributed-systems
summary: "Updating the database and the cache is two writes to two systems, and there is no transaction spanning them. That non-atomicity is the whole problem — here's the classic cache-aside stale-set race as a timeline, the standard fixes and their trade-offs, and why CDC is the most robust answer."
reading_time: 7
tags: [caching, consistency, dual-write, cdc, debezium, cache-invalidation]
sources:
  - title: "Redis — Cache Consistency: Strategies to Keep Data Fresh"
    url: "https://redis.io/blog/cache-consistency-strategies/"
  - title: "Debezium — Automating Cache Invalidation With Change Data Capture"
    url: "https://debezium.io/blog/2018/12/05/automating-cache-invalidation-with-change-data-capture/"
  - title: "Auth0 — Handling the Dual-Write Problem in Distributed Systems"
    url: "https://auth0.com/blog/handling-the-dual-write-problem-in-distributed-systems/"
  - title: "Nishtala et al., Scaling Memcache at Facebook (NSDI 2013)"
    url: "https://www.usenix.org/system/files/conference/nsdi13/nsdi13-final170_update.pdf"
  - title: "redis-developer/sql-cache-invalidation-debezium (reference implementation)"
    url: "https://github.com/redis-developer/sql-cache-invalidation-debezium"
---

A cache in front of a database is two copies of the truth. Keeping them agreed sounds like a caching problem, but it is really an instance of a more general one: the **dual-write problem**. You have to write to two independent systems — the database and the cache — and there is no transaction that spans both. Either write can succeed while the other fails or is delayed, and no amount of careful ordering makes the pair atomic. As Auth0's write-up puts it, once you must "write data to multiple systems atomically" but "can't use atomic transactions," any interleaving of failures leaves the two diverged. Everything below is a strategy for making that divergence *rare and self-healing* rather than *permanent*.

## The race that ruins cache-aside

Cache-aside (lazy loading) is the default read pattern: read from cache; on a miss, read the database and populate the cache; on a write, update the database and then delete the cache key. It works almost always, which is exactly why the failure is so easy to miss.

The dangerous case is a **read miss that overlaps a write**. Here is the timeline for key `user:42`, currently *not* in cache, with DB value `v1`:

```
t0  Reader R  : GET user:42            -> cache MISS
t1  Reader R  : SELECT ... user:42     -> reads v1  (old)
t2  Writer W  : UPDATE ... SET x=v2    -> DB now v2
t3  Writer W  : DEL user:42            -> cache empty (nothing to delete)
t4  Reader R  : SET user:42 = v1       -> cache now holds v1  (STALE)
```

R read the database *before* W committed, then wrote its stale result into the cache *after* W's invalidation had already run. W did everything right and still lost. The cache now serves `v1` while the database says `v2`, and — critically — **nothing corrects it**. The next write might not touch this key for hours. This is the "multi-instance population race" Redis names as cache-aside's signature failure mode, and it is the same stale-set race the Facebook memcache team hit at scale and had to solve with leases.

Note what makes it lethal: the stale value is *durable*. Transient inconsistency is fine; a cache entry that is wrong until the heat death of the TTL is not.

## Delete, don't update

First rule: on a write, **delete the cache key rather than write the new value into it.** Two writers racing to `SET` the cache can commit to the DB in one order and to the cache in the opposite order, leaving the cache pinned to the loser's value. Deleting sidesteps that entirely — a delete is idempotent and order-independent, and the next reader repopulates from the database. Delete also avoids computing and caching a value nobody has asked for yet. Redis and most practitioners treat "invalidate, don't update" as the baseline.

## Order: update DB, *then* delete cache

Given you are deleting, which comes first — the DB write or the cache delete?

**Delete-then-update is worse.** You delete the key, and in the window before your DB commit lands, a concurrent reader misses, reads the *old* DB value, and repopulates the cache — the stale value is back, and now it's durable. **Update-then-delete** shrinks the bad window to the gap between commit and delete, which is milliseconds. So: write the database, commit, *then* delete the key. It is not perfect (the timeline above is still update-then-delete), but it is strictly better and it is the standard cache-aside ordering.

## Delayed double delete

The residual race survives because a slow reader can `SET` a stale value *after* your delete. The pragmatic patch is **delayed double delete**: delete the key, do the write, then schedule a *second* delete a short time later (say 500 ms–1 s).

```python
def update_user(id, patch):
    cache.delete(f"user:{id}")        # optional pre-delete
    db.update(id, patch)              # commit
    cache.delete(f"user:{id}")        # standard post-write delete
    schedule_after(delay_ms=700,      # second delete evicts any
        lambda: cache.delete(f"user:{id}"))   # stale set that snuck in
```

The second delete evicts whatever a lagging reader wrote in the meantime. The delay must exceed a typical read's DB-round-trip; you are trading a tiny extra eviction for closing the window. It is a probabilistic mitigation, not a proof — a reader stalled longer than your delay still loses — but it removes the common case cheaply.

## TTL: the safety net, not the plan

Give every cached entry a TTL. It does not prevent staleness; it *bounds* it. With a 5-minute TTL, the worst case is 5 minutes of wrong data, after which the key expires and reloads. TTL is the backstop that makes every other mechanism forgiving: if a delete is lost, a keyspace event is missed, or a double-delete races badly, the TTL guarantees convergence within a bounded window. Segment it by volatility — minutes for a product catalog, seconds (or a different pattern) for live inventory. Never rely on TTL as your *only* consistency mechanism; rely on it as the floor under everything else.

## Write-through: pay on the write path

Write-through routes writes through the cache, which synchronously updates itself and the database, so readers see the new value immediately (read-your-writes). But it does not escape the dual-write problem — it *relocates* it. The cache and DB writes are still two operations; a partial failure between them leaves them inconsistent and, as Redis notes, requires manual reconciliation. You also pay two-system latency on every write and warm cold data nobody reads. Good for balances and orders where read-your-writes matters; not a free consistency guarantee.

## CDC: derive invalidation from the log (most robust)

Every option above shares one flaw: **the invalidation is a second write the application must remember to make.** Forget it, crash between the two, or let a batch job update the DB directly with `psql`, and the cache never hears about it.

Change Data Capture inverts this. Instead of the application telling the cache, a log-based connector like **Debezium** tails the database's replication log (Postgres WAL, MySQL binlog) and emits one event per *committed* row change. A consumer turns each event into a cache delete. The Debezium team's argument is the key one: "by capturing changes directly from the database log, no events will be missed," because invalidation is *derived from the authoritative source* rather than being a separate, forgettable write. It even catches out-of-band changes made straight to the database. This is the closest thing to escaping the dual-write problem — there is now only *one* write (to the DB), and the cache update is a downstream consequence of the DB's own durable commit record.

```
                      ┌─────────────┐
   app write ───────► │  Postgres   │
                      │   (WAL)     │
                      └──────┬──────┘
                             │  logical decoding
                        ┌────▼─────┐
                        │ Debezium │  one event per committed row
                        └────┬─────┘
                             │  Kafka topic (ordered per key)
                        ┌────▼─────────┐
                        │ cache updater│  DEL user:42  (or SET v2)
                        └────┬─────────┘
                             ▼
                          Redis
```

Two caveats to design for. CDC is **at-least-once**, so consumers must be idempotent — a delete trivially is. And events arrive *after* commit, so this is eventual, not synchronous: expect a lag of milliseconds to low seconds. Debezium orders changes per source table/key, which is what lets a `DEL` and a later `SET` apply in the right order. This is the same architecture as the [transactional outbox](/articles/microservices/2026-07-26-transactional-outbox-pattern) — write the change atomically inside the DB transaction, propagate it asynchronously — and it is exactly the pipeline [Debezium change data capture](/articles/microservices/2026-07-31-debezium-change-data-capture) is built for; the [redis-developer CDC reference project](https://github.com/redis-developer/sql-cache-invalidation-debezium) wires Postgres → Debezium → Redis end to end.

## Versioned keys / CAS

When you *must* write values into the cache (not just delete), carry a version. Store `{value, version}` and only overwrite if the incoming version is greater — a compare-and-set, backed by the DB's monotonically increasing version column or transaction id. A stale reader trying to `SET v1` over `v2` is rejected because `1 < 2`. This directly kills the timeline race: the late write cannot win. The cost is a version on every row and CAS logic in the client (Redis Lua or `WATCH`/`MULTI`). It is the Facebook memcache "leases" idea in a simpler form — attach a token that lets the cache reject an out-of-order set.

## What consistency can you actually buy

You cannot get perfect consistency cheaply. Strong consistency between an independent cache and DB would need a distributed transaction across both on every write — the latency and availability cost is exactly why you added a cache in the first place. So the honest target is **eventual consistency with bounded staleness**:

- **Delete (not update), update-DB-then-delete** — cuts the race window to milliseconds.
- **Delayed double delete** — closes the common slow-reader case.
- **TTL** — bounds worst-case staleness and guarantees convergence.
- **CDC** — makes invalidation reliable and log-derived, not app-remembered.
- **Versioned CAS** — rejects out-of-order writes when you cache values.

The production stance most sources converge on is *layered*: pick the read/write pattern for your workload, put a conservative TTL under it as a backstop, and add an event-driven freshness signal (CDC or keyspace notifications) on top. No single trick is airtight; the combination makes stale data rare, short-lived, and self-correcting — which is the realistic definition of "consistent" here.

**Try next:** implement the delayed-double-delete around your existing cache-aside writes and measure the stale-hit rate before and after; then stand up the Postgres → Debezium → Redis pipeline from the redis-developer reference project and compare its convergence lag against your TTL floor.
