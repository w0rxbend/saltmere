---
title: "Cache versus database: the dual-write problem and its partial solutions"
date: 2026-08-10
track: distributed-systems
summary: "Updating the database and the cache is two writes to two systems with no transaction spanning them. That non-atomicity is the whole problem: the cache-aside stale-set race as a timeline, the standard mitigations and their costs, and why change data capture derives invalidation from the authoritative log."
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

**Gist.** A cache placed in front of a database holds a second copy of the truth, and keeping the two agreed requires writing to two independent systems with **no transaction spanning both** — the dual-write problem. The practical mechanism is not atomicity but convergence: invalidate rather than update, order the database commit before the invalidation, bound the residual staleness with a time-to-live (TTL), and derive the invalidation from the database's own replication log via change data capture (CDC). The cost is that consistency becomes eventual with a bounded staleness window, and every mitigation adds either extra cache traffic, extra write-path latency, or a whole asynchronous pipeline to operate.

## The race that defeats cache-aside

Cache-aside (lazy loading) is the default read pattern: read from the cache; on a miss, read the database and populate the cache; on a write, update the database and then delete the cache key. It behaves correctly under almost every interleaving, which is what makes the exception easy to overlook.

The dangerous case is a **read miss that overlaps a write**. The timeline for key `user:42`, initially absent from the cache, with database value `v1`:

```
t0  Reader R  : GET user:42            -> cache MISS
t1  Reader R  : SELECT ... user:42     -> reads v1  (old)
t2  Writer W  : UPDATE ... SET x=v2    -> DB now v2
t3  Writer W  : DEL user:42            -> cache empty (nothing to delete)
t4  Reader R  : SET user:42 = v1       -> cache now holds v1  (STALE)
```

The reader loaded the database *before* the writer committed and stored its result *after* the writer's invalidation had already executed. The writer performed the prescribed sequence and still lost. The cache now serves `v1` while the database holds `v2`, and **no subsequent event corrects it**: the next write to that key may be hours away. This is the stale-set race that the Facebook memcache deployment addressed with leases: a token handed out on a miss, which the cache checks before accepting the corresponding set (Nishtala et al., NSDI 2013).

The property that makes it damaging is that **the stale entry is durable**. Transient divergence during the write is tolerable; an entry that stays wrong until its TTL expires — or indefinitely, if no TTL is set — is not.

## Invalidate rather than update

The first rule is that a write **deletes the cache key instead of storing the new value**. Two writers racing to `SET` the cache may commit to the database in one order and to the cache in the opposite order, leaving the cache pinned to the value of the transaction that lost at the database. Deletion removes that class of interleaving: **a delete is idempotent and order-independent**, and the next reader repopulates from the authoritative store. Deletion also avoids computing and caching a value no client has requested. Invalidation rather than update is the baseline the Redis cache-consistency guidance recommends.

## Ordering: commit the database, then delete

Given deletion, the remaining choice is which operation precedes the other.

**Delete-then-commit is the worse order.** In the window between the delete and the commit, a concurrent reader misses, reads the *pre-commit* database value, and repopulates the cache; the stale value is restored and is durable. **Commit-then-delete** narrows the vulnerable window to the interval between commit and delete, which is a single cache round trip. It does not eliminate the race — the timeline above is already commit-then-delete — but it strictly dominates the alternative and is the standard cache-aside ordering.

## Delayed double delete

The residual race survives because a reader that started early can `SET` a stale value *after* the writer's delete. The common mitigation is **delayed double delete**: commit the write, delete the key, then schedule a *second* delete a short interval later. (Some descriptions add a further delete before the write; the load-bearing element is the delayed one.)

The second delete evicts whatever a lagging reader stored in the meantime. The delay is chosen to exceed the duration of a typical read, trading one additional eviction (and the miss it causes) for closure of the window. It is **probabilistic, not a proof**: a reader stalled for longer than the configured delay still leaves a stale entry behind.

## TTL as a bound, not as the plan

Every cached entry carries a TTL. A TTL does not prevent staleness; it **bounds its duration**. With a five-minute TTL the worst case is five minutes of incorrect data, after which the key expires and reloads from the database. The TTL is what makes the other mechanisms forgiving: if a delete is lost, a keyspace notification is missed, or a double delete races unfavourably, expiry still guarantees convergence within a bounded window. TTLs are segmented by volatility — longer for slowly changing reference data, shorter for rapidly changing data. A TTL used as the *only* consistency mechanism sets the staleness bound equal to the TTL itself.

## Write-through: cost moved to the write path

Write-through routes writes through the cache, which synchronously updates itself and the database, so readers observe the new value immediately (read-after-write visibility). It does not escape the dual-write problem; it **relocates** it. The cache write and the database write remain two operations, and a failure between them leaves the pair inconsistent, with nothing in the pattern itself to reconcile them. Write-through also pays two-system latency on every write and populates entries no reader requests. It suits workloads where read-after-write visibility is required, such as balances and orders; it is not a consistency guarantee.

## CDC: invalidation derived from the log

Every option above shares one defect: **the invalidation is a second write that the application must remember to issue**. If the application omits it, crashes between the two operations, or a batch job modifies the database directly, the cache is never informed.

Change data capture inverts the direction. A log-based connector such as **Debezium** tails the database's replication log — the PostgreSQL write-ahead log (WAL) or the MySQL binary log — and emits one event per *committed* row change; a consumer converts each event into a cache delete. Because the log is the record the database itself commits against, a change that is committed is a change the connector sees. The invalidation is **derived from the authoritative commit record** rather than issued as a separate, omissible write, so out-of-band modifications made straight against the database are captured as well. The application performs a single write, and the cache update becomes a downstream consequence of the database's own durable log.

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

Two properties constrain the design. Delivery is **at-least-once**, so consumers must be idempotent — a delete is. And events are emitted *after* commit, making the result eventual rather than synchronous, with a propagation lag. Events carry the row's primary key as the message key, and a log-backed transport preserves order within a partition, so changes to the same row reach the consumer in commit order. The architecture matches the [transactional outbox](/articles/microservices/2026-07-26-transactional-outbox-pattern) — record the change atomically inside the database transaction, propagate it asynchronously — and it is the pipeline [Debezium change data capture](/articles/microservices/2026-07-31-debezium-change-data-capture) implements; the [redis-developer CDC reference project](https://github.com/redis-developer/sql-cache-invalidation-debezium) wires Postgres to Debezium to Redis end to end.

## Versioned keys and compare-and-set

Where values must be written into the cache rather than only deleted, each entry carries a version. The cache stores `{value, version}` and accepts an overwrite only if the incoming version is greater — a compare-and-set (CAS) backed by a monotonically increasing version column or transaction identifier from the database. A stale reader attempting to store `v1` over `v2` is rejected because `1 < 2`, which removes the timeline race above: the late write cannot win. The costs are a version attribute on every row and CAS logic in the client, expressed in Redis as a Lua script or a `WATCH`/`MULTI` transaction. It plays the same role as the memcache lease token: a value the cache checks in order to reject an out-of-order set.

### Implementation sketch (Scala)

```scala
final case class Versioned[A](value: A, version: Long)

// Compare-and-set population: a late reader cannot overwrite a newer entry.
def populate[A](
    cache: TrieMap[String, Versioned[A]],
    key: String,
    incoming: Versioned[A]
): Boolean =
  cache.get(key) match
    case Some(current) if current.version >= incoming.version => false
    case Some(current) =>
      // replace only if nothing changed underneath between read and write
      cache.replace(key, current, incoming) || populate(cache, key, incoming)
    case None =>
      cache.putIfAbsent(key, incoming).isEmpty || populate(cache, key, incoming)

// Commit-then-delete, with a second delete after the reader round-trip window.
def write[A](key: String, commit: () => Unit, delete: String => Unit,
             schedule: (FiniteDuration, () => Unit) => Unit,
             delay: FiniteDuration): Unit =
  commit()                                   // database is authoritative first
  delete(key)
  schedule(delay, () => delete(key))         // evicts a stale set that arrived late
```

## What consistency is purchasable

Strong consistency between an independent cache and a database requires a distributed transaction across both on every write; that latency and availability cost is the reason the cache exists. The attainable target is **eventual consistency with bounded staleness**:

- **Delete rather than update, commit before deleting** — reduces the race window to one cache round trip.
- **Delayed double delete** — closes the common lagging-reader case, probabilistically.
- **TTL** — bounds worst-case staleness and guarantees convergence.
- **CDC** — makes invalidation log-derived rather than application-remembered.
- **Versioned CAS** — rejects out-of-order sets when values are cached.

The stance the cited sources converge on is layered: select the read/write pattern for the workload, place a conservative TTL beneath it as a backstop, and add an event-driven freshness signal (CDC or keyspace notifications) above it. No single mechanism is airtight; the combination makes stale entries rare, short-lived and self-correcting.

## Pitfalls

- **Writing the new value into the cache instead of deleting it.** Symptom: the cache holds the value of a transaction that lost at the database. Cause: two writers commit in one order and set the cache in the reverse order.
- **Deleting the key before the database commit.** Symptom: the stale value returns immediately and persists. Cause: a concurrent reader misses during the window, reads the pre-commit value, and repopulates.
- **Treating delayed double delete as a proof.** Symptom: occasional durable stale entries despite the second delete. Cause: a reader stalled longer than the configured delay stores its value after both deletes.
- **Caching without a TTL.** Symptom: a single lost invalidation produces an entry that is wrong indefinitely. Cause: no expiry exists to force convergence when the invalidation path fails.
- **Assuming write-through removes the dual write.** Symptom: cache and database disagree after a partial failure, with no reconciliation path in the pattern. Cause: the cache write and database write remain two non-atomic operations.
- **Non-idempotent CDC consumers.** Symptom: duplicated side effects on redelivery. Cause: CDC delivery is at-least-once; only operations such as delete tolerate repetition unchanged.
- **Expecting CDC invalidation to be synchronous.** Symptom: a client reads its own write and receives the pre-write cached value. Cause: events are emitted after commit, so the cache converges with a propagation lag.
- **Applying CAS without a monotonic version source.** Symptom: comparisons accept an older value. Cause: the version does not increase monotonically per row, so `<` no longer identifies the stale write.
