---
title: "Isolation levels and MVCC: what actually goes wrong, level by level"
date: 2026-08-13
track: distributed-systems
summary: "The ANSI isolation levels are defined by the anomalies they forbid — dirty reads, non-repeatable reads, phantoms — but the interview-grade anomaly is write skew, which snapshot isolation permits and serializable forbids. Here's the anomaly table, how MVCC implements snapshots with xmin/xmax, and a two-terminal psql demo of write skew."
reading_time: 6
tags: [isolation-levels, mvcc, snapshot-isolation, write-skew, postgres]
sources:
  - title: "PostgreSQL docs — Transaction Isolation"
    url: "https://www.postgresql.org/docs/current/transaction-iso.html"
  - title: "Berenson et al., A Critique of ANSI SQL Isolation Levels (SIGMOD 1995)"
    url: "https://www.cs.cmu.edu/~15721-f24/papers/Critique_of_ANSI_Isolation_Levels.pdf"
  - title: "Ports & Grittner, Serializable Snapshot Isolation in PostgreSQL (VLDB 2012)"
    url: "https://arxiv.org/abs/1208.4179"
  - title: "PostgreSQL wiki — SSI (Serializable Snapshot Isolation)"
    url: "https://wiki.postgresql.org/wiki/SSI"
  - title: "Vlad Mihalcea — A beginner's guide to the write skew anomaly"
    url: "https://vladmihalcea.com/write-skew-2pl-mvcc/"
---

Isolation levels are not four speeds on a dial; they're a list of specific race conditions a database promises to prevent. Learn the anomalies and the levels define themselves — that's how the standard is written, and it's how the interview question is graded.

## The anomaly zoo

- **Dirty read** — you read a value another transaction wrote but hasn't committed; it may roll back, so you read data that never existed.
- **Non-repeatable read** — you read a row twice in one transaction and get different committed values, because someone committed in between.
- **Phantom** — you run the same *predicate* query twice (`WHERE dept = 'x'`) and rows appear or vanish; the rows you read didn't change, the membership did.
- **Lost update** — two transactions read-modify-write the same row; the second commit silently overwrites the first.
- **Write skew** — two transactions each read an overlapping set, then write to *disjoint* rows, each decision valid against its snapshot but jointly violating an invariant. No single row was contended, so nothing conflicts.

Berenson et al. (1995) showed the ANSI definitions were too weak — they define levels by the first three anomalies only, which lets **snapshot isolation** slip through: SI exhibits none of the ANSI three yet is not serializable, precisely because of write skew.

## Levels vs anomalies

| Level | Dirty read | Non-repeatable read | Phantom | Lost update | Write skew |
|---|---|---|---|---|---|
| Read uncommitted | possible* | possible | possible | possible | possible |
| Read committed | no | possible | possible | possible | possible |
| Repeatable read (ANSI) | no | no | possible | no | possible |
| Snapshot isolation | no | no | no† | no | **possible** |
| Serializable | no | no | no | no | no |

\* Postgres has no true read uncommitted; requesting it gives read committed. † Postgres's `REPEATABLE READ` *is* snapshot isolation, so it also prevents phantoms — stronger than ANSI requires. The row that matters is the SI one: everything prevented except write skew.

## How MVCC delivers this

Multi-version concurrency control never updates a row in place. In Postgres, every row version carries two hidden system columns: `xmin`, the transaction ID that created the version, and `xmax`, the transaction ID that deleted or superseded it (0 while live). An `UPDATE` is a delete+insert: the old version gets its `xmax` stamped, a new version is written with a fresh `xmin`.

A transaction takes a **snapshot**: the set of transaction IDs committed as of some instant. Visibility is then a pure function — a version is visible if `xmin` is committed-in-snapshot and `xmax` is absent or not-committed-in-snapshot. Read committed takes a new snapshot per *statement*; repeatable read takes one per *transaction*. Readers never block writers and writers never block readers, which is the whole selling point over lock-based two-phase locking.

```sql
SELECT xmin, xmax, ctid, * FROM accounts WHERE id = 1;
UPDATE accounts SET balance = balance - 10 WHERE id = 1;
SELECT xmin, xmax, ctid, * FROM accounts WHERE id = 1;  -- new xmin, new ctid
```

The cost: superseded versions ("dead tuples") pile up in the heap. **VACUUM** reclaims versions no live snapshot can see — and it's also why long-running transactions are operationally dangerous: they pin the horizon, vacuum can't clean behind them, and tables bloat.

## Write skew, live at REPEATABLE READ

The canonical demo: a hospital requires at least one doctor on call. Two are on call; each tries to book off after checking the invariant.

```sql
CREATE TABLE doctors (name text PRIMARY KEY, on_call bool);
INSERT INTO doctors VALUES ('alice', true), ('bob', true);

-- Terminal 1                                 -- Terminal 2
BEGIN ISOLATION LEVEL REPEATABLE READ;
SELECT count(*) FROM doctors
  WHERE on_call;                -- 2, safe
                                              BEGIN ISOLATION LEVEL REPEATABLE READ;
                                              SELECT count(*) FROM doctors
                                                WHERE on_call;        -- 2, safe
UPDATE doctors SET on_call = false
  WHERE name = 'alice';
COMMIT;
                                              UPDATE doctors SET on_call = false
                                                WHERE name = 'bob';
                                              COMMIT;                 -- succeeds!

SELECT count(*) FROM doctors WHERE on_call;   -- 0. Invariant broken.
```

Both transactions saw a consistent snapshot; they wrote different rows, so SI's write-write conflict check ("first committer wins" applies only to the *same* row) never fires. Rerun the same script with `ISOLATION LEVEL SERIALIZABLE` and the second `COMMIT` fails with SQLSTATE `40001` — retry it and the retry sees one doctor on call and refuses.

## SSI: serializable without serial execution

Postgres 9.1+ implements **Serializable Snapshot Isolation** (Cahill's algorithm, productionized by Ports & Grittner). It runs transactions on plain snapshots but additionally tracks reads with non-blocking **SIREAD locks** (including predicate/index-range granularity, which is how phantoms and write skew through predicates get caught). It watches for **rw-antidependencies** — T1 read something T2 later wrote. Theory says every SI anomaly requires two consecutive rw-antidependencies in the conflict graph; when SSI sees that "dangerous structure," it aborts one transaction with `40001`.

The contract this imposes on application code: *any* serializable transaction can be rejected even without touching the same rows as anyone else, so you must wrap them in a retry loop. False positives exist (the check is conservative), but there's no blocking and, per the VLDB paper, modest overhead versus plain SI. Compare that to the pessimistic alternative — 2PL takes shared locks on everything read and blocks instead of aborting.

Interview closer: "repeatable read in Postgres" and "repeatable read in MySQL/InnoDB" are different animals (InnoDB's uses next-key locking for writes and can still exhibit its own quirks), so always answer in terms of anomalies, not level names.

**Try next:** run the doctors demo in two psql terminals at REPEATABLE READ, then at SERIALIZABLE, and inspect the SIREAD locks mid-flight with `SELECT locktype, relation::regclass, mode FROM pg_locks WHERE mode = 'SIReadLock';`.
