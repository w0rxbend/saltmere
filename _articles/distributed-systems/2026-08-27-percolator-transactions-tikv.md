---
title: "Percolator: Snapshot-Isolation Transactions on a Key-Value Store, as TiKV Runs Them Today"
date: 2026-08-27
track: distributed-systems
summary: Percolator builds multi-row snapshot-isolation transactions from the only primitive Bigtable offers — atomic mutation of a single row — by threading two-phase commit through lock, write, and data columns and electing one key's lock as the commit point. TiKV still runs the protocol over RocksDB column families. Covers prewrite, commit, lazy cleanup of crashed writers by their readers, and the standing costs — read-path lock resolution and two replicated writes per committed key.
reading_time: 8
tags:
- percolator
- tikv
- snapshot-isolation
- two-phase-commit
- transactions
- bigtable
sources:
- title: Peng & Dabek — Large-scale Incremental Processing Using Distributed Transactions and Notifications (OSDI 2010)
  url: https://www.usenix.org/legacy/event/osdi10/tech/full_papers/Peng.pdf
- title: TiKV Deep Dive — Percolator
  url: https://tikv.org/deep-dive/distributed-transaction/percolator/
- title: TiKV Deep Dive — Optimized Percolator
  url: https://tikv.org/deep-dive/distributed-transaction/optimized-percolator/
---

**Gist.** Bigtable guarantees atomicity for mutations to a single row and nothing wider; Percolator (Peng & Dabek, OSDI 2010) builds cross-row, cross-machine snapshot-isolation transactions on top of that one primitive by running two-phase commit through extra columns — `lock`, `write`, `data` — and designating one key's lock as the **primary**, whose atomic replacement by a commit record *is* the commit point. TiKV runs the same protocol today over RocksDB column families. The costs are structural: every committed key is written twice (prewrite, then commit), and every reader that meets a leftover lock must stop and resolve it before it can proceed.

## The substrate: per-row atomicity and a timestamp oracle

Percolator was built to update Google's web index incrementally rather than by full MapReduce rebuilds; the paper reports it **reduced the average age of documents in search results by 50%** while processing the same crawl. The transactional layer it needed did not exist in Bigtable, and the designers chose not to modify Bigtable itself. The protocol therefore assumes only two services:

- a storage layer that can apply a batch of mutations to **one row atomically** — Bigtable's row transaction, or in TiKV a Raft-replicated write batch applied to one region;
- a **timestamp oracle (TSO)** handing out strictly increasing timestamps. Every transaction draws a `start_ts` when it begins and a `commit_ts` when it commits, and all versioning and conflict detection is expressed in these two numbers. The oracle is a single logical service, so Percolator batches timestamp requests to keep it off the critical path.

Each logical column is physically three columns. For a user column `c`, the store keeps `c:data` (the value, versioned by `start_ts`), `c:lock` (an uncommitted transaction's lock), and `c:write` (the commit record: at version `commit_ts`, a pointer to the `start_ts` whose data it commits). In TiKV these become three RocksDB column families — `CF_DEFAULT`, `CF_LOCK`, `CF_WRITE` — each a separate log-structured merge (LSM) tree.

## Prewrite: locks first, one of them primary

A transaction buffers its writes client-side until commit. Commit then runs classic two-phase commit, but with the transaction's own keys as the coordination state — there is no separate coordinator log to lose.

**Prewrite.** One written key is chosen as the **primary**; the rest are secondaries that record the primary's location inside their locks. For each key, in a single atomic per-row mutation, the client:

1. checks `write` for any commit with `commit_ts > start_ts` — if found, another transaction committed after this one's snapshot, a **write–write conflict**, and the transaction aborts;
2. checks `lock` for any existing lock at any timestamp — if found, a concurrent writer holds the key and this transaction backs off or aborts;
3. writes the value into `data` at `start_ts` and a lock into `lock` naming the primary.

If any key's prewrite fails, the transaction rolls back by deleting the locks and data it already wrote — a lock plus its `data` entry can always be removed safely, because nothing committed refers to them yet.

**Commit.** The client draws `commit_ts` from the oracle, then performs one atomic mutation on the primary: verify its lock still exists, delete it, and write a record into `write` at `commit_ts` pointing back to `start_ts`. **That single per-row mutation is the commit point.** If it succeeds, the transaction is durably committed regardless of what happens next; the secondaries are then committed the same way, but lazily and without any urgency — the paper and the TiKV documentation both state that failure while committing secondaries does not matter.

## Reads, and readers cleaning up the dead

A read at snapshot `ts` on key `k`:

1. checks `lock` for a lock with timestamp ≤ `ts`. If one exists, the visible state of `k` is undecided — the locking transaction might commit below `ts` — so the reader **cannot proceed past it**;
2. otherwise finds the latest `write` record with `commit_ts ≤ ts` and reads `data` at the `start_ts` it names.

Step 1 is where crash recovery lives. Percolator worker processes fail without warning, leaving locks behind, and there is no coordinator to time them out. Instead, **cleanup is lazy and performed by the reader that trips over the lock**. The blocked reader follows the stuck lock to its primary and inspects that one row, whose state is authoritative because commit was atomic there:

- **primary lock still present** → the writer never committed. The reader rolls the transaction back by removing the primary lock (and, transitively, the secondaries).
- **primary lock gone, commit record present** → the writer committed and died mid-cleanup. The reader **rolls the transaction forward**, writing the missing commit record for the stuck secondary.

Because either resolution is itself a per-row atomic mutation on the primary, two readers racing to clean up the same transaction cannot disagree. Distinguishing a crashed writer from a merely slow one is the delicate part: Percolator has cleaners check liveness tokens in Chubby plus a wall-time lease before killing a lock; TiKV attaches a **time-to-live (TTL)** to each lock, and a reader may resolve a lock only after its TTL expires — before that it backs off and retries.

### Implementation sketch (Scala)

The state machine a reader executes against a stuck lock:

```scala
enum Resolution:
  case RolledBack, RolledForward, StillAlive

def resolve(lock: Lock, store: RowStore, now: Long): Resolution =
  if now < lock.expiresAt then Resolution.StillAlive  // back off, retry
  else
    // The primary row is the single source of truth: commit was one
    // atomic mutation there, so exactly one branch below holds.
    store.atomically(lock.primaryKey) { row =>
      row.lockAt(lock.startTs) match
        case Some(_) =>                       // never committed
          row.removeLock(lock.startTs)
          row.removeData(lock.startTs)
          Resolution.RolledBack
        case None =>
          row.commitRecordFor(lock.startTs) match
            case Some(commitTs) =>            // committed, cleanup died
              store.atomically(lock.key) { sec =>
                sec.removeLock(lock.startTs)
                sec.putWrite(commitTs, lock.startTs)
              }
              Resolution.RolledForward
            case None => Resolution.RolledBack // already rolled back
        }
    }
```

## The bill: two writes per key and reads that block on writers

The protocol's costs follow directly from its shape.

**Two replicated writes per committed key.** Prewrite writes `data` + `lock`; commit deletes the lock and writes `write`. In TiKV each of those is a Raft-replicated write batch, so a committed key costs **two consensus rounds** where a non-transactional store would pay one. TiKV's documented optimizations shave constants rather than the shape: **short values are inlined** — embedded in the lock at prewrite, then moved into the write record at commit — so point reads of small values never touch `CF_DEFAULT` at all; prewrite batches run in parallel because rollback records in `CF_WRITE` fence late-arriving prewrites; and a point read of the newest version can skip allocating a `start_ts` from the TSO entirely.

**Read-path lock resolution.** Snapshot isolation usually promises readers never block, but here a reader can stall behind any writer whose locks overlap its snapshot — for the full lock TTL if the writer crashed at the worst moment. A long-running or wedged transaction converts into latency for every reader of its keys. This is the standing operational cost of having no coordinator: recovery work was moved from a dedicated component onto the read path of innocent bystanders.

**Everything ends at the TSO.** Both timestamps for every transaction come from one logical allocator, which makes it a scalability and availability chokepoint; batching amortizes throughput but a TSO outage stops all new transactions.

## Pitfalls

- **A lock in `CF_LOCK` is not evidence the transaction failed** — deleting it before checking the primary can roll back a transaction that already committed, breaking atomicity; only the primary row's state decides.
- **Resolving a lock before its TTL expires kills live writers** — a slow but healthy transaction is aborted by an impatient reader, and under contention this becomes mutual cancellation.
- **A committed transaction is invisible on secondaries until roll-forward happens** — a reader that finds a secondary's lock and gives up (instead of consulting the primary) reports a stale value for data that is durably committed.
- **Write–write conflict detection is only at prewrite** — two transactions with the same snapshot that write the same key are serialized by lock acquisition, so snapshot isolation's write-skew anomaly on *disjoint* write sets remains permitted, exactly as in any snapshot-isolation system.
- **The TSO sits on every transaction's critical path twice** — once for `start_ts`, once for `commit_ts` — so an oracle outage stops all new commits even while storage nodes are healthy.
