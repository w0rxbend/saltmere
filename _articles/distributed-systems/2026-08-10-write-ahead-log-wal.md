---
title: 'Write-Ahead Logging: How Databases Survive a Crash Mid-Update'
date: 2026-08-10
track: distributed-systems
summary: A crash in the middle of updating a data page leaves that page half-written and the database corrupt. Write-ahead logging fixes this with one rule — describe the change in a sequential, append-only log and fsync it BEFORE touching the data page ("log first"). This walks the durability problem, the WAL rule, redo vs undo logging, the LSN, checkpoints and ARIES-style analysis→redo→undo recovery, group commit's throughput-vs-latency trade, the torn-page problem and Postgres full_page_writes, and how the same log becomes the source for replication and CDC. Includes a minimal append-only WAL plus replay.
reading_time: 7
tags:
- write-ahead-log
- durability
- crash-recovery
- aries
- databases
- wal
- postgres
sources:
- title: 'ARIES: A Transaction Recovery Method... Using Write-Ahead Logging (Mohan, Haderle, Lindsay, Pirahesh, Schwarz, ACM TODS 1992)'
  url: https://web.stanford.edu/class/cs345d-01/rl/aries.pdf
- title: 'PostgreSQL Documentation: 30.3. Write-Ahead Logging (WAL) — Introduction'
  url: https://www.postgresql.org/docs/current/wal-intro.html
- title: 'PostgreSQL Documentation: 30.1. Reliability (torn pages, full_page_writes)'
  url: https://www.postgresql.org/docs/current/wal-reliability.html
- title: 'PostgreSQL Documentation: 19.5. Write Ahead Log (commit_delay, group commit, wal_sync_method)'
  url: https://www.postgresql.org/docs/current/runtime-config-wal.html
- title: Write-ahead logging and the ARIES crash recovery algorithm (Kevin Sookocheff)
  url: https://sookocheff.com/post/databases/write-ahead-logging/
- title: 'Mohan et al., ARIES: A Transaction Recovery Method (ACM TODS, 1992)'
  url: https://dl.acm.org/doi/10.1145/128765.128770
- title: Hironobu Suzuki, The Internals of PostgreSQL — Ch. 9, WAL
  url: https://www.interdb.jp/pg/pgsql09.html
- title: RocksDB wiki — Write Ahead Log File Format
  url: https://github.com/facebook/rocksdb/wiki/Write-Ahead-Log-File-Format
---

**Gist.** Updating data pages in place is efficient for reads but has no crash story: a power loss mid-flush leaves a page that is neither the old version nor the new one, and nothing on disk can distinguish the two. Write-ahead logging (WAL) imposes a single ordering constraint — a record describing a change reaches durable storage before the corresponding data page is modified on disk — so that recovery can reconstruct the committed state from the log. The cost is one durable sequential append (and its `fsync`) on the commit path, plus a log whose volume must be bounded by checkpoints and whose replay must be idempotent.

## The durability problem

Consider a transfer that subtracts 100 from one account row and adds 100 to another. Both rows live in 8 KiB data pages held in a buffer pool; the operating system flushes those pages to storage at some later, unspecified time. A power failure during that window produces two distinct faults.

The first is **partial durability**: one page reached stable storage and the other did not, violating the invariant that total balance is conserved. The second is a **torn page** — the device was partway through writing *one* page when power was lost. The PostgreSQL reliability documentation states the mechanism directly: a write "could fail due to power loss at any time, meaning some of the 512-byte sectors were written while others were not." An 8 KiB page spans 16 such sectors, so **the number of distinct on-disk outcomes is bounded above by 2^16, and any outcome mixing sectors whose contents differ between the two generations is a page that belongs to neither** — undetectable without external redundancy, because the page header, including any version stamp, may itself be from the wrong generation. (Sectors the update left byte-identical contribute no bad outcomes, so the count of corrupt results depends on how much of the page the write changed.)

## The WAL rule: log first

The rule is an ordering constraint between two storage writes. Before a data page containing a modification is written to disk, a record *describing* that modification must already have been forced to permanent storage. PostgreSQL states it as:

> changes to data files ... must be written only after those changes have been logged, that is, after WAL records describing the changes have been flushed to permanent storage.

The performance argument follows from device physics. The log is **sequential and append-only**: appending a record of tens to hundreds of bytes and forcing it costs one rotational or one flash-program latency, with no seek and no read-modify-write. Random writes of dirty 8 KiB pages can then be deferred and batched arbitrarily, because the log already holds the authoritative record. A commit reduces to *append, force, acknowledge*: O(1) sequential I/O operations per transaction rather than O(pages touched) random ones. **Durability is purchased with a cheap sequential write; the expensive random writes become a background concern.**

## Redo, undo, and the buffer policies that require them

A log record can describe a change in two directions, and general-purpose engines carry both.

- **Redo** records carry the after-image, or a physiological operation that reproduces it. They allow recovery to reapply committed work that never reached the data pages.
- **Undo** records carry the before-image, enough to reverse a change. They serve both live `ROLLBACK` and recovery of transactions in flight at the crash.

Which is mandatory follows from the buffer-manager policy, in the taxonomy ARIES uses. Under **steal**, a dirty page of an uncommitted transaction may be evicted, so its effects can survive a crash without a commit — **undo is required**. Under **no-force**, commit does not flush the transaction's data pages, so committed effects may be absent from disk — **redo is required**. ARIES targets steal/no-force and needs both; a no-steal/force engine needs neither, paying instead with pinned buffers and synchronous page flushes at commit.

## The log sequence number and idempotent replay

Every log record receives a monotonically increasing **log sequence number (LSN)**, typically a byte offset into the logical log so ordering and location coincide. Each data page stores in its header the LSN of the most recent record that modified it, the `pageLSN`. This pairing yields the central invariant of recovery:

> For every page P on disk, every logged change with LSN ≤ `P.pageLSN` is reflected in P, and no change with LSN > `P.pageLSN` is.

Redo therefore tests, per record and per page, whether `pageLSN >= record.LSN`. If so the change is already present and is skipped; otherwise it is applied and `pageLSN` is advanced to `record.LSN`. Because the test is per page, replaying the entire log is **idempotent**: applying it once, twice, or being interrupted partway and restarted yields the same state. Recovery is thus restartable, which matters because recovery itself can crash.

## Checkpoints and the three ARIES passes

Unbounded replay makes restart time proportional to database lifetime. **Checkpoints** bound it. A fuzzy checkpoint records the dirty page table and the transaction table without quiescing the system; the resulting `RedoLSN` is the minimum `recLSN` over dirty pages — the earliest log position whose effects might not be on disk. Restart cost is then O(bytes of log after `RedoLSN`), controlled directly by the checkpoint interval.

ARIES (Mohan, Haderle, Lindsay, Pirahesh and Schwarz, *ACM TODS* 17(1), 1992) recovers in three passes:

1. **Analysis** — scan forward from the last checkpoint, reconstructing the dirty page table and the set of transactions active at the crash (the *losers*), and computing `RedoLSN`.
2. **Redo — repeat history.** Replay *all* logged changes from `RedoLSN` forward, including those of loser transactions, using the `pageLSN` test to skip work already present. The database is restored to its exact state at the instant of the crash.
3. **Undo** — roll back the losers by following each transaction's backward `prevLSN` chain. Each reversal is itself logged as a **compensation log record (CLR)**, which carries an `UndoNxtLSN` pointing past the record it compensates.

Redoing uncommitted work only to undo it is what makes the algorithm uniform: pass 2 need not distinguish winners from losers, and pass 3 operates on a known state. CLRs are never undone, so **a crash during undo resumes from the last CLR rather than repeating rollback work; undo of a transaction is therefore performed at most once per logical operation regardless of how many times recovery is interrupted.**

### Implementation sketch (Scala)

The load-bearing detail is the position of the force relative to the page mutation, and the `pageLSN` test during replay.

```scala
final case class Record(lsn: Long, pageId: Int, key: String, before: String, after: String)

final class Wal(ch: java.nio.channels.FileChannel, pages: scala.collection.mutable.Map[Int, Page]):
  private var next: Long = 1L

  def append(pageId: Int, key: String, before: String, after: String): Long =
    val r = Record(next, pageId, key, before, after)
    next += 1
    ch.write(encode(r))
    ch.force(false)          // metadata excluded; the record must be durable HERE
    val p = pages(pageId)    // only now may the in-memory page change
    p.data(key) = after
    p.pageLSN = r.lsn        // page and log agree again
    r.lsn

// Redo pass: forward scan, skipping changes a page already reflects.
def redo(records: Iterator[Record], pages: scala.collection.mutable.Map[Int, Page]): Unit =
  for r <- records do
    val p = pages.getOrElseUpdate(r.pageId, Page.empty)
    if p.pageLSN < r.lsn then
      p.data(r.key) = r.after
      p.pageLSN = r.lsn      // the invariant: pageLSN bounds what the page contains

// Undo pass: backward over losers only, emitting compensation records.
def undo(losers: List[Record], wal: Wal): Unit =
  for r <- losers.reverse do
    wal.append(r.pageId, r.key, before = r.after, after = r.before)  // a CLR
```

A crash after `ch.force` but before the page mutation loses nothing: redo reconstructs the value. A crash before the append means the record was never acknowledged, and its absence is correct. Production engines add checksums, `prevLSN` chaining and physiological redo, but the ordering and the LSN test are the whole mechanism.

## Group commit

The forced log write bounds commit throughput at one durable append per transaction: a few hundred per second on a rotational device, a few thousand on flash without a write cache. **Group commit** amortises it — the engine delays committing transactions briefly and forces once for the whole batch, converting *n* forces into one. PostgreSQL exposes the delay as `commit_delay`, applied only when at least `commit_siblings` other transactions are active. The trade: added latency of at most the delay per transaction, for aggregate throughput that scales with concurrency until log bandwidth, not force rate, becomes the limit.

## Torn pages and full-page writes

Redo requires reading a page and trusting its `pageLSN`. A torn page invalidates that premise: the header may be from either generation, so the test returns an arbitrary answer and redo has no sound base state. PostgreSQL's remedy is to write **a full image of the page into the WAL the first time the page is modified after each checkpoint**, before the page itself is written; the documentation describes this as restoring "partially-written pages from WAL." That is `full_page_writes`. The cost is quantifiable: the first modification of each page after a checkpoint contributes 8 KiB of WAL rather than a delta of tens of bytes, so WAL volume spikes after every checkpoint and lengthening the checkpoint interval reduces total WAL volume for a page-repeating workload. Storage that guarantees atomic page writes (copy-on-write filesystems such as ZFS) removes the need.

`wal_sync_method` selects the syscall used to force the log (`fdatasync`, `open_datasync`, `fsync`); `O_DIRECT` bypasses the kernel page cache so the engine, not the writeback path, governs ordering.

## The log as a stream

Because the WAL is a complete, totally ordered record of every change, it also serves as the change feed. Physical replication ships and replays segments verbatim; PostgreSQL **logical decoding** reconstructs row-level changes from the same records, as MySQL's binary log (binlog) does. The recovery journal and the replication feed are one file read for two purposes.

For where the WAL sits inside the storage engine, see [LSM-Trees vs B-Trees](/articles/distributed-systems/2026-08-10-lsm-trees-vs-b-trees) — both rely on a WAL for durability. For pipelines built on the log, see [Debezium change data capture](/articles/microservices/2026-07-31-debezium-change-data-capture).

## WAL vs command logging

| | Physical/physiological WAL (ARIES, Postgres) | Command logging (VoltDB-style) |
|---|---|---|
| What is logged | Page/tuple-level effects (before and after images) | The transaction's command and parameters |
| Log volume | Proportional to bytes changed | Proportional to request size |
| Recovery cost | Apply effects; no re-execution | Re-execute every command since the last snapshot |
| Requirement | None | Commands must be **deterministic** |
| Fits | General-purpose, ad-hoc SQL | In-memory stores with stored-procedure workloads |

Command logging answers "log less" at the price of determinism: a transaction that reads the wall clock, draws a random value, or interleaves nondeterministically diverges on replay. That is the requirement replicated state machines impose in Raft and Paxos — a replicated log and a recovery log are one construction aimed at two failure models.

## Pitfalls

- **`fsync` returns success yet data is lost after a power cut.** A disk write cache acknowledged the write before it reached stable media; the barrier must reach the device, which requires write-cache flushing to be enabled end to end (drive, controller, virtualisation layer).
- **Recovery reports checksum failures on pages that were never written by the crashed transaction.** `full_page_writes` was disabled on storage that does not guarantee atomic page writes, so a torn page has no full image in the log to restore from.
- **Commit throughput collapses under concurrency while disk utilisation stays low.** Each transaction is forcing the log alone; without group commit the ceiling is the device's force rate, not its bandwidth.
- **Restart takes hours on a database that was healthy at shutdown time.** Checkpoints were too infrequent or blocked by long-running dirty pages, leaving `RedoLSN` far behind the log tail; restart time is linear in the log bytes after `RedoLSN`.
- **Replay applies a change twice and corrupts a counter.** The redo operation was relative (an increment) and ran without the `pageLSN` test, breaking idempotence.
- **Disk fills although the database is small.** WAL segments are pinned by a stale replication slot or a failing archiver, so they cannot be recycled after checkpointing.
- **`synchronous_commit = off` raises throughput with no measured change in WAL volume, and a crash loses committed transactions.** The setting removes the force from the commit path, not the logging; acknowledged transactions within the flush window are lost while the database remains internally consistent.
