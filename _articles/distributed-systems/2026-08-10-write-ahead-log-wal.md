---
title: 'Write-Ahead Logging: How Databases Survive a Crash Mid-Update'
date: 2026-08-10
track: distributed-systems
summary: A crash in the middle of updating a data page leaves that page half-written and the database corrupt. Write-ahead logging fixes this with one rule — describe the change in a sequential, append-only log and fsync it BEFORE touching the data page ("log first"). This walks the durability problem, the WAL rule, redo vs undo logging, the LSN, checkpoints and ARIES-style analysis→redo→undo recovery, group commit's throughput-vs-latency trade, the torn-page problem and Postgres full_page_writes, and how the same log becomes the source for replication and CDC. Includes a minimal append-only WAL plus replay.
reading_time: 6
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
- title: 'PostgreSQL Documentation: 28.3. Write-Ahead Logging (WAL) — Introduction'
  url: https://www.postgresql.org/docs/current/wal-intro.html
- title: 'PostgreSQL Documentation: 28.1. Reliability (torn pages, full_page_writes)'
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

"How does your database survive `kill -9` in the middle of a write?" is a question with exactly one production answer, and every relational engine — PostgreSQL, InnoDB, SQLite, Oracle — gives the same one. This is the write-ahead log.

## The durability problem

Picture a bank transfer: subtract 100 from Alice's balance, add 100 to Bob's. Both balances live in an 8 KB data page cached in memory. You mutate the page and the OS eventually flushes it to disk. Now the power fails mid-flush.

Two things can go wrong. First, **partial durability**: Alice's page reached the platter but Bob's did not, so 100 has vanished from the ledger. Second, and worse, **partial page writes** — the disk was 8 KB of the way through writing *one* page when the power died. Postgres puts it bluntly: a write "could fail due to power loss at any time, meaning some of the 512-byte sectors were written while others were not." That page is now neither the old version nor the new one. It is garbage, and there is no log-free way to even detect it.

Updating data pages in place is fast for reads but has no crash story. WAL adds one.

## The WAL rule: log first

The rule is a strict ordering constraint. Before you modify a data page on disk, you must first write a record *describing* that change to a separate log, and that log record must be durable — `fsync`'d to permanent storage — before the page write is allowed to proceed. Postgres states it exactly:

> changes to data files ... must be written only after those changes have been logged, that is, after WAL records describing the changes have been flushed to permanent storage.

Why this helps: the log is a **sequential, append-only** file. Appending and fsync'ing a small contiguous record is one of the fastest things a disk does — no seeks, no read-modify-write. The expensive part, scattering random 8 KB pages across the disk, can now be deferred and batched, because the log already holds the ground truth. As the docs note, "we do not need to flush data pages to disk on every transaction commit, because ... we will be able to recover the database using the log." A commit becomes: append records, fsync the log, return. The data pages catch up lazily.

That inversion — durability comes from a cheap sequential write, not an expensive random one — is the entire performance argument for WAL.

## Redo vs undo

A log record can describe a change two ways, and mature systems carry both.

- **Redo** records store the *new* value (or a physical/logical operation that reproduces it). They let recovery *reapply* committed work that never made it to the data pages. Redo is what makes deferred page flushing safe.
- **Undo** records store enough to *reverse* a change — the *old* value. They let recovery (or a live `ROLLBACK`) erase the effects of transactions that were in flight at crash time but never committed.

You need undo because of a "steal" buffer policy: a dirty page from an *uncommitted* transaction may get evicted to disk before commit. After a crash, that change sits in the data file and must be rolled back from the log. Redo covers "committed but not yet on the page"; undo covers "on the page but never committed." ARIES uses both.

## The LSN

Every log record gets a monotonically increasing **Log Sequence Number**. The LSN is the backbone that ties log to pages: each data page stores, in its header, the LSN of the last log record that modified it (`pageLSN`). This single number drives the central optimization of recovery — **idempotent replay**.

During recovery you scan the log forward and, for each redo record, compare its LSN to the target page's `pageLSN`. If `pageLSN >= record.LSN`, the page already reflects this change; skip it. If `pageLSN < record.LSN`, reapply and advance `pageLSN`. Because the check is per-page, you can replay the whole log safely no matter how far each individual page had progressed before the crash. Recovery is *repeatable*.

## Checkpoints and ARIES recovery

If recovery replayed the log from the beginning of time, a long-lived database would take days to restart. **Checkpoints** bound this. Periodically the system flushes dirty pages and writes a checkpoint record noting which transactions were active and the oldest log position still needed. Recovery starts there, not at LSN 0.

ARIES (Mohan et al., 1992 — still the reference algorithm) recovers in three passes:

1. **Analysis** — scan forward from the last checkpoint to rebuild the set of dirty pages and the list of transactions in flight at the crash.
2. **Redo** — "repeat history." Replay *all* logged changes (even uncommitted ones) from the earliest dirty-page LSN forward, using the `pageLSN` check to skip work already on disk. This restores the database to its exact state at the moment of the crash.
3. **Undo** — roll back the transactions that Analysis found were never committed, walking their undo records backward. ARIES logs these rollbacks too, as **compensation log records (CLRs)**, so that a crash *during* recovery doesn't lose progress.

The counterintuitive move is redoing uncommitted work in pass 2 only to undo it in pass 3. Doing so makes the algorithm uniform: history is replayed exactly, then losers are backed out.

## A minimal append-only WAL

The mechanism fits in a few lines. Log first, fsync, then apply — and on restart, replay:

```python
import json, os

class WAL:
    def __init__(self, path, page):
        self.f = open(path, "ab", buffering=0)  # append-only
        self.page = page                        # the "data page" (a dict)
        self.lsn = 0

    def write(self, key, new, old):
        self.lsn += 1
        rec = {"lsn": self.lsn, "key": key, "new": new, "old": old}
        self.f.write((json.dumps(rec) + "\n").encode())
        os.fsync(self.f.fileno())   # <-- durable BEFORE the page changes
        self.page[key] = new        # only now touch the data page

def recover(path):
    page = {}
    with open(path, "rb") as f:
        for line in f:                       # scan forward = REDO
            r = json.loads(line)
            page[r["key"]] = r["new"]         # idempotent: last write wins
    return page
```

The ordering in `write()` is the whole point: `fsync` sits *between* the log append and the page mutation. If the process dies after the append but before `self.page[key] = new`, `recover()` reconstructs the value from the log. If it dies before the append, the change never happened — which is correct, the client never got a commit acknowledgement. Real engines add undo, LSN-based skip, and checksums, but the skeleton is this.

## Group commit: throughput vs latency

The `fsync` per commit is the throughput ceiling — a spinning disk does a few hundred fsyncs/sec. **Group commit** amortizes it: instead of each transaction fsync'ing alone, the engine holds a batch of committing transactions for a few microseconds and flushes all their log records with *one* fsync. Ten transactions, one sync. Postgres exposes this as `commit_delay` and `commit_siblings`. The trade is explicit — a sliver of added latency per transaction for far higher aggregate throughput under concurrency. It is the classic batching dial.

## The torn-page problem and full_page_writes

Redo assumes it can read a data page, check its `pageLSN`, and reapply. But if a crash tore that page mid-write, its header — and thus its LSN — is corrupt, and redo has nothing sound to build on. Postgres's fix: the first time a page is modified after each checkpoint, it writes a **full image of the entire page** into the WAL. The docs: it "periodically writes full page images to permanent WAL storage *before* modifying the actual page on disk," so recovery can "restore partially-written pages from WAL." That is the `full_page_writes` parameter. It bloats the WAL (a full 8 KB image, not a small delta), which is why the first writes after a checkpoint are the heaviest — but it is what makes recovery robust against torn pages. If your filesystem already prevents partial writes (e.g. ZFS), you can disable it.

Related knobs: `wal_sync_method` picks the syscall used to force the log to disk, and `O_DIRECT` bypasses the OS page cache so the engine controls its own durability rather than trusting the kernel's writeback.

## The log is also a stream

One more payoff: because the WAL is a complete, ordered record of *every* change, it is the perfect source for anything that needs to observe changes. Physical replicas simply ship and replay the WAL. And change-data-capture reads it too — PostgreSQL **logical decoding** turns WAL records back into logical row changes, and MySQL's **binlog** plays the same role. Tools like Debezium tail this log to feed downstream systems without dual-writes. The crash-recovery journal and the replication/CDC feed are the same file, read twice.

For where the WAL sits inside the storage engine, see [LSM-Trees vs B-Trees](/articles/distributed-systems/2026-08-10-lsm-trees-vs-b-trees) — both lean on a WAL for durability. For building pipelines on top of the log, see [Debezium change data capture](/articles/microservices/2026-07-31-debezium-change-data-capture).

**Try next:** disable `full_page_writes` on a scratch Postgres, pull the plug (or `kill -9` mid-`pgbench`), and see whether recovery still succeeds — then turn it back on and watch the WAL volume spike right after each checkpoint.

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
