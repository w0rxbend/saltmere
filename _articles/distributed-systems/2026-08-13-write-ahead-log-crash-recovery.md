---
title: "Write-ahead logging: the one rule that makes crash recovery possible"
date: 2026-08-13
track: distributed-systems
summary: "Every durable database rests on one invariant: the log record describing a change reaches stable storage before the data page it describes. From that rule you get fsync costs, group commit, checkpoints, and ARIES-style redo/undo — plus the knobs Postgres exposes to trade latency for durability."
reading_time: 5
tags: [wal, crash-recovery, aries, postgres, durability]
sources:
  - title: "Mohan et al., ARIES: A Transaction Recovery Method (ACM TODS, 1992)"
    url: "https://dl.acm.org/doi/10.1145/128765.128770"
  - title: "PostgreSQL docs — Write-Ahead Logging (WAL)"
    url: "https://www.postgresql.org/docs/current/wal-intro.html"
  - title: "PostgreSQL docs — Write Ahead Log configuration"
    url: "https://www.postgresql.org/docs/current/runtime-config-wal.html"
  - title: "Hironobu Suzuki, The Internals of PostgreSQL — Ch. 9, WAL"
    url: "https://www.interdb.jp/pg/pgsql09.html"
  - title: "RocksDB wiki — Write Ahead Log File Format"
    url: "https://github.com/facebook/rocksdb/wiki/Write-Ahead-Log-File-Format"
---

A database can't fsync every dirty page on every commit — that's random I/O all over the disk. So it buffers pages in memory and writes them back lazily. Which raises the interview question this article answers: if the machine dies with dirty pages unflushed, how does anything survive? The answer is a sequential, append-only log plus one invariant.

## The WAL rule

Two invariants, really:

1. **Log-before-data.** Before a modified data page is written to disk, all log records describing changes to that page must already be on stable storage.
2. **Commit rule (force-log-at-commit).** Before a transaction is acknowledged as committed, all of its log records — including the commit record — must be on stable storage.

Every log record gets a monotonically increasing **LSN** (log sequence number), and every data page stores the LSN of the last record applied to it (`pageLSN`). Enforcement is then one comparison:

```
write_page(P):
    if P.pageLSN > flushedLSN:      # log not durable yet
        flush_log(up_to = P.pageLSN)
    disk.write(P)

commit(T):
    append(log, CommitRecord(T))
    flush_log(up_to = T.commitLSN)  # the fsync you pay for
    reply_ok(T)
```

Why it works: anything that made it to a data page is provably re-derivable from (or already in) the log, and anything acknowledged to a client is in the log. The log is sequential I/O, so you've converted "fsync scattered pages" into "fsync one append-only file" — the same trick the LSM-tree article's memtable relies on.

## fsync and group commit

That commit-time flush is the durability tax: an fsync costs ~50 µs–1 ms on NVMe, milliseconds on spinning disks or cloud block storage. **Group commit** amortizes it — while one fsync is in flight, other backends queue their commit records, and the next fsync hardens all of them at once. One disk flush, N commits. Throughput scales; per-commit latency stays roughly one fsync.

Postgres exposes the trade explicitly via `synchronous_commit`:

```ini
# postgresql.conf
wal_level = replica          # minimal | replica | logical
synchronous_commit = on      # off: ack before WAL flush; window of loss
                             # is up to 3 * wal_writer_delay (~600 ms),
                             # but never corruption — the log is still ordered
full_page_writes = on        # torn-page protection (below)
commit_delay = 0             # µs to linger before flushing, widens group commit
```

`synchronous_commit = off` is the classic interview trade-off: you can lose the last few hundred milliseconds of acknowledged commits after a crash, but the database recovers to a *consistent* prefix — no corruption, because ordering is preserved. It's per-transaction settable, so you can make audit writes synchronous and clickstream writes async in the same database.

## Checkpoints and ARIES-style recovery

Without checkpoints, recovery replays the log from the beginning of time. A **checkpoint** bounds that: flush (or at least record) the state of dirty pages and active transactions, then note the checkpoint's position in the log. Recovery starts near the last checkpoint instead of at LSN 0.

ARIES (Mohan et al., 1992) is the canonical recovery algorithm, and its three passes are worth being able to recite:

1. **Analysis** — scan forward from the last checkpoint; rebuild the set of dirty pages and of transactions that were live at the crash ("losers").
2. **Redo** — *repeat history*: reapply every logged change whose `pageLSN` shows it hasn't reached the page yet — including changes by loser transactions. This restores the exact pre-crash state.
3. **Undo** — roll back the losers, newest change first, writing **compensation log records** (CLRs) for each undo so that a crash *during recovery* never undoes the same thing twice.

"Redo everything, then undo losers, logging the undos" is the whole outline. The CLR detail is what interviewers probe: it makes recovery idempotent.

## WAL in Postgres and in LSM engines

**Postgres** keeps WAL as 16 MB segment files in `pg_wal/`. `wal_level` controls how much is logged (`replica` for physical replication and PITR, `logical` adds enough for logical decoding). One subtlety: `full_page_writes`. OS pages are 4 KB but Postgres pages are 8 KB, so a crash mid-write can leave a **torn page** that per-tuple redo can't fix. Postgres therefore logs the *entire page image* the first time a page is touched after each checkpoint — which is also why aggressive checkpointing inflates WAL volume. The same WAL stream doubles as the replication feed: physical standbys are just continuous crash recovery.

**LSM engines** (RocksDB, Cassandra) make the WAL even more central: a write goes to the WAL and to the in-memory memtable, and that's it — there are no data pages to flush at all. When the memtable is written out as an immutable SST file, the corresponding WAL segment becomes garbage and is deleted. Recovery is simply "rebuild the memtable by replaying the live WAL." Same invariant, simpler mechanics, because sorted-run files are never modified in place.

## WAL vs command logging

| | Physical/physiological WAL (ARIES, Postgres) | Command logging (VoltDB-style) |
|---|---|---|
| What's logged | Page/tuple-level effects (before+after images) | The transaction's command + parameters |
| Log volume | Larger | Tiny |
| Recovery | Fast: apply effects, no re-execution | Slow: re-execute every command since snapshot |
| Requirement | None special | Commands must be **deterministic** |
| Fits | General-purpose, ad-hoc SQL | In-memory stores with stored-procedure workloads |

Command logging is a legitimate answer to "can we log less?" — but the moment a transaction reads the clock, a random number, or interleaves nondeterministically, replay diverges. That determinism requirement is the same one Raft-style replicated state machines impose, which is no coincidence: a replicated log and a recovery log are the same idea pointed at different failure modes.

**Try next:** set `synchronous_commit = off` on a scratch Postgres, run `pgbench` before and after, and watch commit throughput jump while `pg_stat_wal` shows identical WAL volume — then read `pg_waldump` output for one of your own transactions.
