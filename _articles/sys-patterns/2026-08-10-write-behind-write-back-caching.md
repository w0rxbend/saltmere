---
title: "Write-behind caching in depth: absorb the writes, flush them later"
date: 2026-08-10
track: sys-patterns
summary: "Write-behind turns the cache into a write buffer: put() returns the instant the value lands in memory, and a background flusher batches dirty entries to the database on a timer. You get write throughput and coalescing for free — and inherit durability, ordering, and read-your-writes problems that make it the advanced option."
reading_time: 7
tags:
  - caching
  - write-behind
  - write-back
  - durability
  - backpressure
  - go
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

The [read/write strategies overview](/articles/microservices/2026-08-10-caching-strategies-read-write-patterns/) on this journal lines up six caching patterns side by side. This article zooms all the way in on the most operationally demanding of them: **write-behind**, also called **write-back**. It is the one that buys the most throughput and charges the most in return.

## What write-behind actually does

In [write-through](/articles/microservices/2026-08-10-caching-strategies-read-write-patterns/), a `put()` writes the cache *and* the database synchronously, then returns. The caller pays the full database latency on every write, and the database sees one write per `put()`.

Write-behind breaks that coupling. A `put()` writes the cache, records the key as **dirty**, and returns immediately. A background worker later drains the dirty set and writes those entries to the backing store — "after a configurable delay, whether after 10 seconds, 20 minutes, a day or even a week or longer," as Oracle's Coherence docs put it. The cache is, for that window, the system of record.

The CPU does exactly this. A write-back L1/L2 cache marks a line dirty and only writes it to main memory on eviction; a write-through cache pushes every store down immediately. Same trade in silicon that we make in a service: fewer, later writes to the slow tier in exchange for a window where the fast tier holds the only current copy.

## Why you would take on the risk

Three payoffs, all of them about the write path:

1. **Write throughput / latency.** The caller only waits for an in-memory insert. Database round-trip latency leaves the hot path entirely.
2. **Write coalescing.** This is the headline. If a key is updated five times inside the flush window, the database sees the *final* value once, not five times. Coherence: "multiple changes to the same object within the write-behind interval are 'coalesced' and only written once." A counter hammered 10,000 times a second becomes one row update per flush.
3. **Batching / smoothing DB load.** The flusher can combine many dirty keys into one bulk statement or transaction — Coherence calls this "write-combining" via `storeAll()` — turning a spiky per-request write pattern into steady, controllable bursts the database can plan around.

Coalescing and batching are why write-behind can cut database write volume by orders of magnitude on update-heavy, hot-key workloads. That is the whole reason to reach for it.

## The mechanics: a dirty set plus a timed batch flusher

The core is a map of pending writes (the dirty set, keyed so a second update to a key **replaces** the first — that is coalescing), a flush triggered by *either* a batch-size threshold *or* a time window, and back-pressure when the queue fills. Ehcache 3 exposes exactly these knobs: batch size, a **maximum write delay** ("after this time has elapsed, the batch is processed even if incomplete"), coalescing ("only send the latest mutation on a per key basis"), and a queue size that "applies back pressure on cache operations" when exceeded.

Here is a batching write-behind buffer in Go with coalescing, interval flush, and back-pressure:

```go
type WriteBehind struct {
	mu        sync.Mutex
	dirty     map[string][]byte // key -> latest value (coalescing)
	maxDirty  int               // back-pressure threshold
	batchSize int               // max keys per DB flush
	store     func(map[string][]byte) error
	full      *sync.Cond
}

// Put updates the cache-visible value and marks the key dirty.
// A second Put to the same key overwrites the pending one: N updates -> 1 write.
func (w *WriteBehind) Put(key string, val []byte) {
	w.mu.Lock()
	for len(w.dirty) >= w.maxDirty {
		w.full.Wait() // back-pressure: block writers until the flusher drains
	}
	w.dirty[key] = val
	w.mu.Unlock()
}

// flushLoop runs on a ticker: time-window trigger.
func (w *WriteBehind) flushLoop(interval time.Duration, stop <-chan struct{}) {
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-t.C:
			w.flushOnce()
		case <-stop:
			w.flushOnce() // drain on shutdown — critical for durability
			return
		}
	}
}

func (w *WriteBehind) flushOnce() {
	w.mu.Lock()
	if len(w.dirty) == 0 {
		w.mu.Unlock()
		return
	}
	batch := make(map[string][]byte, w.batchSize)
	for k, v := range w.dirty {
		batch[k] = v
		delete(w.dirty, k)
		if len(batch) >= w.batchSize {
			break
		}
	}
	w.full.Broadcast() // room freed — wake blocked writers
	w.mu.Unlock()

	if err := w.store(batch); err != nil {
		// store() failed: re-queue so we don't lose data (only if not superseded).
		w.mu.Lock()
		for k, v := range batch {
			if _, superseded := w.dirty[k]; !superseded {
				w.dirty[k] = v
			}
		}
		w.mu.Unlock()
	}
}
```

The load-bearing details: writes to a key **overwrite** the pending entry rather than appending, so the map *is* the coalescing mechanism; the flush fires on a ticker but also caps each batch at `batchSize` so one flush can't stall on a giant transaction; `full.Wait()` gives real back-pressure so an infinite write rate can't grow the buffer without bound; and the failed-store path **re-queues** entries that a newer write hasn't already superseded, so a transient DB outage doesn't silently drop data. Note that the re-queue reorders writes relative to arrival — which is a segue into the risks.

## The risks that make it advanced

**Durability / data loss on crash.** Between a `put()` returning and the next flush, the only copy of that update lives in memory. If the process dies, those writes are gone. Coherence is blunt about it: write-behind "effectively makes the cache the system-of-record (until the write-behind queue has been written to disk)," so you need "cluster-durable (rather than disk-durable)" storage and business rules that tolerate it. Mitigations: replicate the queue to backup nodes, shorten the window, or persist the queue to a log — each of which claws back some of the latency win. This is why you never put write-behind in front of a payment ledger.

**Ordering.** Coalescing and batching mean the database is updated out of arrival order, and possibly not at all for intermediate states. Coherence warns that CacheStore operations must be **idempotent** and that referential-integrity constraints "must allow for out-of-order updates." If a downstream system tails the database expecting every state transition, write-behind will disappoint it.

**Read-your-writes.** Because the database lags the cache, a read that bypasses the cache and hits the database can return a *stale* value — the pre-write state. Write-behind is only consistent if **reads go through the same cache that holds the dirty entries**. A second service reading the DB directly, a replica, or an analytics query will see old data until the flush lands. Design the read path and the write path as one unit, or don't use write-behind.

There is also the transactional gap: the cache transaction commits before the database transaction begins, so "the database transactions must never fail; if this cannot be guaranteed, then rollbacks must be accommodated" (Coherence). You have moved the failure from the caller's face to a background worker where nobody is watching — instrument the flusher's error rate and queue depth accordingly.

## Write-through vs write-behind, in one table

| | Write-through | Write-behind |
|---|---|---|
| `put()` latency | DB write latency | in-memory only |
| DB writes | one per put | coalesced + batched |
| Durability on crash | safe (DB already has it) | at risk until flush |
| Ordering | preserved | may reorder / skip states |
| Reads from DB directly | consistent | can be stale |
| Good for | correctness-critical writes | hot keys, high write rate, tolerant of a lag window |

Real systems ship both as a choice you flip per cache: Coherence and Ehcache both let you configure a `CacheStore`/`CacheLoaderWriter` as write-through or write-behind against the same code. The pattern is generic; the decision is about how much of a durability and consistency window your data can survive.

**Try next:** wire the Go buffer above to a real store, then chaos-test it — kill the process mid-window and measure exactly how many acknowledged writes you lose at flush intervals of 1s, 10s, and 60s. Then revisit the [read/write strategies overview](/articles/microservices/2026-08-10-caching-strategies-read-write-patterns/) and map each failure you saw back to the risk that predicted it.
