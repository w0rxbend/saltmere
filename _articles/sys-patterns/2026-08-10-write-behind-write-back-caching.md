---
title: "Write-behind caching in depth: absorb the writes, flush them later"
date: 2026-08-10
track: sys-patterns
summary: "Write-behind turns the cache into a write buffer: put() returns once the value lands in memory, and a background flusher batches dirty entries to the database on a timer. It buys write throughput and coalescing, and inherits durability, ordering, and read-after-write consistency problems in exchange."
reading_time: 7
tags:
  - caching
  - write-behind
  - write-back
  - durability
  - backpressure
sources:
  - title: "Oracle Coherence — Read-Through, Write-Through, Write-Behind, and Refresh-Ahead Caching"
    url: "https://docs.oracle.com/cd/E16459_01/coh.350/e14510/readthrough.htm"
  - title: "Ehcache 3.8 — Cache Loaders and Writers (write-behind, batching, coalescing)"
    url: "https://www.ehcache.org/documentation/3.8/writers.html"
  - title: "Oracle Coherence 3.5 (O'Reilly) — Write behind"
    url: "https://www.oreilly.com/library/view/oracle-coherence-35/9781847196125/ch08s05.html"
  - title: "GeeksforGeeks — Write Through and Write Back in Cache"
    url: "https://www.geeksforgeeks.org/computer-organization-architecture/write-through-and-write-back-in-cache/"
---

**Gist.** Update-heavy workloads make the database absorb one write per application write, and the caller pays the database round trip each time. Write-behind (also called write-back) decouples the two: `put()` mutates the cache, marks the key **dirty**, and returns, while a background flusher drains dirty entries to the backing store on a batch-size or time trigger, collapsing repeated updates to the same key into a single store operation. The cost is a window during which the cache is the only holder of the current value — a window in which a crash loses acknowledged writes, the database observes updates out of arrival order, and any reader that bypasses the cache sees stale state.

The [read/write strategies overview](/articles/microservices/2026-08-10-caching-strategies-read-write-patterns/) on this journal lines up six caching patterns side by side. This article examines the most operationally demanding of them.

## What write-behind does

In [write-through](/articles/microservices/2026-08-10-caching-strategies-read-write-patterns/), a `put()` writes the cache *and* the database synchronously before returning. The caller pays the full database latency on every write, and the database sees one write per `put()`.

Write-behind breaks that coupling. A `put()` writes the cache, records the key as dirty, and returns. A background worker later drains the dirty set and writes those entries to the backing store — "after a configurable delay, whether after 10 seconds, 20 minutes, a day or even a week or longer," in the wording of Oracle's Coherence documentation. For the length of that delay, **the cache is the system of record**.

Processor caches implement the same distinction. A write-back level-1 or level-2 (L1/L2) cache marks a line dirty and defers the store to main memory until the line is evicted; a write-through cache pushes every store down immediately. The trade is identical: fewer and later writes to the slow tier, in exchange for an interval during which the fast tier holds the only current copy.

## What the delay buys

Three effects, all on the write path.

1. **Write latency.** The caller waits only for an in-memory insert. The database round trip leaves the hot path.
2. **Write coalescing.** If a key is updated several times inside the flush window, the database receives the *final* value once. Coherence states that "multiple changes to the same object within the write-behind interval are 'coalesced' and only written once." A repeatedly incremented counter becomes one row update per flush regardless of the update rate.
3. **Batching.** The flusher combines many dirty keys into one bulk statement or transaction — in Coherence the flusher hands the batch to the `CacheStore`'s `storeAll()` method — converting a per-request write pattern into periodic bursts.

Coalescing and batching are the reason write-behind can reduce database write volume substantially on update-heavy, hot-key workloads. The reduction factor is bounded by the number of repeat updates per key per window: a key touched *n* times within one window produces one store instead of *n*.

## Mechanism: a dirty map plus a triggered batch flusher

The structure has three parts. First, a map of pending writes keyed by cache key, where a second update **replaces** rather than appends — the map *is* the coalescing mechanism, and its invariant is that it holds at most one entry per key, always the latest. Second, a flush fired by *either* a batch-size threshold *or* an elapsed-time threshold, so neither a quiet period nor a burst can starve the store. Third, a bound on queue size that applies back-pressure to writers once reached.

Ehcache 3.8 exposes these as explicit configuration on its write-behind writer: a batch size, a maximum write delay that bounds how long an operation waits before being written, coalescing that reduces the queued operations for a key to the latest one, and a queue size limit that applies back-pressure on cache operations once reached.

Two consequences follow from the design rather than from any implementation choice. Because a failed store must be retried, and because a retried entry may have been superseded in the meantime, the **retry path must not overwrite a newer pending value** — otherwise a transient database failure resurrects a stale write. And because retried batches are re-submitted after batches that arrived later, **the store observes writes out of arrival order**.

### Implementation sketch (Scala)

```scala
final class WriteBehind[K, V](
    maxDirty: Int,
    batchSize: Int,
    storeAll: Map[K, V] => Unit
):
  private val lock  = new Object
  private var dirty = Map.empty[K, V]

  /** Blocks once the queue is full: back-pressure, not unbounded growth. */
  def put(key: K, value: V): Unit = lock.synchronized {
    while dirty.size >= maxDirty do lock.wait()
    dirty = dirty.updated(key, value) // replace, not append -> coalescing
  }

  def flushOnce(): Unit =
    val batch = lock.synchronized {
      val b = dirty.take(batchSize)
      dirty = dirty -- b.keys
      lock.notifyAll() // room freed
      b
    }
    if batch.nonEmpty then
      try storeAll(batch)
      catch case _: Exception =>
        lock.synchronized {
          // Re-queue only keys no newer put() has superseded.
          dirty = batch.foldLeft(dirty) { case (m, (k, v)) =>
            if m.contains(k) then m else m.updated(k, v)
          }
        }
```

The load-bearing lines are the `updated` in `put` (one entry per key, latest wins), the `take(batchSize)` cap (one flush cannot expand into an unbounded transaction), the `wait`/`notifyAll` pair (writers block instead of growing the buffer), and the `contains` guard in the retry path (a re-queued entry never overwrites a newer one). Shutdown must call `flushOnce` until the map is empty; a process that exits with a non-empty dirty map discards acknowledged writes.

## Failure modes

**Loss on crash.** Between `put()` returning and the next successful flush, the only copy of the update is in memory. Coherence records that write-behind "effectively makes the cache the system-of-record (until the write-behind queue has been written to disk)," and that this requires "cluster-durable (rather than disk-durable)" storage plus business rules that tolerate the exposure. Replicating the queue to backup nodes, shortening the window, or persisting the queue to a log each reduce the exposure and each return part of the latency saving.

**Ordering.** Coalescing removes intermediate states entirely, and batching plus retry reorders the surviving ones. Coherence requires that `CacheStore` operations be **idempotent** and that referential-integrity constraints "must allow for out-of-order updates." A downstream consumer that tails the database expecting every state transition will not observe them.

**Read-after-write consistency.** The database lags the cache, so a read that bypasses the cache returns the pre-write value. Write-behind is consistent only if **every read goes through the cache holding the dirty entries**. A second service querying the database, a read replica, or an analytics job will see the older state until the flush lands.

**Transaction gap.** The cache "transaction" commits before the database transaction begins. Coherence states the consequence directly: "the database transactions must never fail; if this cannot be guaranteed, then rollbacks must be accommodated." The failure has moved from the caller's response to a background worker, where it is visible only through the flusher's error rate and queue depth.

## Write-through versus write-behind

| | Write-through | Write-behind |
|---|---|---|
| `put()` latency | database write latency | in-memory only |
| Database writes | one per put | coalesced and batched |
| Durability on crash | committed before return | at risk until flush |
| Ordering | preserved | may reorder or skip states |
| Reads issued directly to the database | consistent | can be stale |
| Suited to | correctness-critical writes | hot keys, high write rate, tolerant of a lag window |

Both Coherence and Ehcache expose the choice as configuration over the same user code — a `CacheStore` or `CacheLoaderWriter` implementation runs unchanged in either mode. The pattern is generic; the decision is how large a durability and consistency window the data can survive.

## Pitfalls

- **Exiting without draining the queue.** The process terminates with a non-empty dirty map and acknowledged writes vanish, because nothing outside the cache ever held them.
- **Unbounded queue.** With no `maxDirty` limit, a write rate exceeding flush throughput grows the buffer until the heap is exhausted; the symptom is an out-of-memory failure under sustained load rather than at the moment of overload.
- **Retry overwriting a newer value.** A failed batch re-queued without the supersession check restores an older value over a newer pending one, and the database ends up with a value no caller ever wrote last.
- **Non-idempotent store operations.** A batch that partially succeeded and is retried whole applies increments or inserts twice; Coherence's idempotence requirement exists precisely because retries are unavoidable.
- **A second reader on the database.** A reporting query, replica, or sibling service reads pre-flush state and reports values the cache-facing service considers committed.
- **Unmonitored flusher.** Store errors no longer surface to callers, so a persistently failing flusher looks like a healthy service until the queue depth or a crash exposes the backlog.
- **Foreign-key or uniqueness constraints on the target table.** Reordered flushes insert a child row before its parent and the batch fails as a unit, stalling every key it contained.
