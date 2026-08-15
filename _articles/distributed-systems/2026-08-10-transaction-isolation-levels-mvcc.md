---
title: 'Isolation levels & MVCC: the anomalies, not the names, are the spec'
date: 2026-08-10
track: distributed-systems
summary: 'The ''I'' in ACID, defined properly. Isolation levels are named by which read phenomena they forbid — dirty read, non-repeatable read, phantom — but the SQL standard''s names are a trap: Postgres has no real read-uncommitted, its ''repeatable read'' is snapshot isolation, and MySQL InnoDB defaults to repeatable read. MVCC lets readers not block writers, but snapshot isolation still permits write skew and lost update. Here''s the anomaly/level matrix, a concrete write-skew example, and the SERIALIZABLE / SELECT FOR UPDATE fixes.'
reading_time: 6
tags:
- transactions
- isolation-levels
- mvcc
- snapshot-isolation
- write-skew
- serializable
- postgres
sources:
- title: Kleppmann, Designing Data-Intensive Applications, Ch. 7 (Weak Isolation & Serializability)
  url: https://dataintensive.net/
- title: Berenson, Bernstein, Gray, Melton, O'Neil, O'Neil — A Critique of ANSI SQL Isolation Levels (SIGMOD 1995)
  url: https://www.microsoft.com/en-us/research/wp-content/uploads/2016/02/tr-95-51.pdf
- title: PostgreSQL Documentation — 13.2. Transaction Isolation
  url: https://www.postgresql.org/docs/current/transaction-iso.html
- title: MySQL 8.0 Reference Manual — 17.7.2.1 Transaction Isolation Levels
  url: https://dev.mysql.com/doc/refman/8.0/en/innodb-transaction-isolation-levels.html
- title: The Morning Paper — A Critique of ANSI SQL Isolation Levels (summary)
  url: https://blog.acolyer.org/2016/02/24/a-critique-of-ansi-sql-isolation-levels/
- title: Berenson et al., A Critique of ANSI SQL Isolation Levels (SIGMOD 1995)
  url: https://www.cs.cmu.edu/~15721-f24/papers/Critique_of_ANSI_Isolation_Levels.pdf
- title: Ports & Grittner, Serializable Snapshot Isolation in PostgreSQL (VLDB 2012)
  url: https://arxiv.org/abs/1208.4179
- title: PostgreSQL wiki — SSI (Serializable Snapshot Isolation)
  url: https://wiki.postgresql.org/wiki/SSI
- title: Vlad Mihalcea — A beginner's guide to the write skew anomaly
  url: https://vladmihalcea.com/write-skew-2pl-mvcc/
---

Ask a candidate "what does the I in ACID mean" and you'll hear "transactions don't interfere." True but useless. Isolation is a *spectrum*, and the interview question underneath it is always: **which concurrency anomalies does this level allow, and what does that cost me?** Get the anomalies right and the four SQL isolation levels fall out for free — because the levels are literally *defined* by which anomalies they forbid.

## The read phenomena

The ANSI SQL standard names three phenomena. They are the vocabulary; learn them precisely.

- **Dirty read** — you read a row another transaction wrote but has not committed. If that transaction rolls back, you read data that never existed.
- **Non-repeatable read** — you read a row, another transaction commits an *update* to it, you read it again in the *same* transaction and get a different value. The row changed under you.
- **Phantom read** — you run a query with a predicate (`WHERE status = 'pending'`), another transaction *inserts* a row matching that predicate and commits, you re-run the query and a new row appears. Non-repeatable read is about a row's *value* changing; a phantom is about the *set of matching rows* changing.

DDIA adds two the ANSI standard misses, and interviewers love them because they survive levels that "sound safe":

- **Lost update** — two transactions read a value, both modify it based on what they read, both write back. One write silently clobbers the other (the classic read-modify-write counter increment).
- **Write skew** — a generalization of lost update. Two transactions read an overlapping set of rows, each makes a decision based on what it read, and each writes to a *different* row. Individually every write is legal; together they violate an invariant that spanned both reads. More on this below — it's the anomaly that breaks snapshot isolation.

## The four levels, by what they forbid

Each level is a strictly stronger promise: it forbids everything weaker forbids, plus one more phenomenon.

| Isolation level | Dirty read | Non-repeatable read | Phantom read | Serialization anomaly (write skew) |
| --- | --- | --- | --- | --- |
| Read uncommitted | Allowed | Allowed | Allowed | Allowed |
| Read committed | Prevented | Allowed | Allowed | Allowed |
| Repeatable read | Prevented | Prevented | Allowed* | Allowed |
| Serializable | Prevented | Prevented | Prevented | Prevented |

That's the textbook matrix. Now the nuance that separates a strong answer from a memorized one.

## The names lie — real databases differ

The Berenson et al. 1995 paper ("A Critique of ANSI SQL Isolation Levels") is the key citation here. Its argument: the ANSI standard defines levels by prose descriptions of phenomena that are *ambiguous*, and worse, the phenomena as written don't capture snapshot isolation at all. Real engines diverged accordingly, so the level names are not portable across databases.

**PostgreSQL** implements only three distinct levels internally:

- It has **no true read-uncommitted** — request it and you get read-committed. Under MVCC there is simply no way to read an uncommitted version, so "dirty read" is impossible in Postgres at *any* level.
- Its **"repeatable read" is snapshot isolation**, which is *stronger* than the standard requires. The Postgres docs are explicit: its repeatable read "prevents all of the phenomena described... except for serialization anomalies," and it **does not allow phantom reads** — hence the asterisk in the matrix above. Providing a stronger guarantee than the standard mandates is legal, because the standard says which anomalies must *not* occur, not which *must*.
- **Serializable** (since 9.1) uses Serializable Snapshot Isolation (SSI): snapshot isolation plus runtime monitoring of read/write dependencies, aborting a transaction with `could not serialize access due to read/write dependencies` when the interleaving has no equivalent serial order.

**MySQL InnoDB** defaults to **repeatable read** (Postgres and most others default to read-committed — a real gotcha when porting). InnoDB's repeatable read uses consistent-read snapshots for plain `SELECT`, but for locking reads it applies **next-key locks** (row lock + gap lock) that lock the gaps between index records, which prevents phantoms for those locking statements. So "repeatable read" means materially different things in Postgres versus MySQL.

The lesson for the interview: **never say "repeatable read prevents X" without naming the database.** Say "the standard permits phantoms at repeatable read, but Postgres's snapshot-isolation implementation forbids them and MySQL blocks them for locking reads via next-key locks."

## MVCC: why readers don't block writers

Multi-version concurrency control is the machinery under all of this. Instead of overwriting a row in place, the database keeps **multiple committed versions**, each tagged with the transaction that created it. On write, it appends a new version; the old one stays visible to transactions that started earlier.

When a transaction begins (or issues its first read, depending on level), it captures a **snapshot**: the set of transaction IDs committed as of that instant. Every read filters versions to "the latest committed before my snapshot, not created by an in-flight transaction." The payoff: a reader never waits for a writer and vice versa — they touch different versions, so analytics queries run against a live OLTP database without blocking writes. Read-committed takes a *fresh* snapshot per statement; snapshot isolation (repeatable read) takes *one* snapshot for the whole transaction, which is exactly what makes reads repeatable.

## Where snapshot isolation breaks: write skew

Snapshot isolation is powerful but **not serializable**. The canonical counterexample (DDIA's on-call doctors) shows why. Rule: at least one doctor must stay on call per shift. Two doctors, Alice and Bob, both on call, both feeling sick, both click "go off call" at the same instant.

```sql
-- Transaction A (Alice)              -- Transaction B (Bob), concurrent
BEGIN;                                BEGIN;
SELECT count(*) FROM doctors          SELECT count(*) FROM doctors
  WHERE on_call = true                  WHERE on_call = true
  AND shift_id = 1234;                  AND shift_id = 1234;
-- returns 2  → safe to leave         -- returns 2  → safe to leave
UPDATE doctors SET on_call = false    UPDATE doctors SET on_call = false
  WHERE name = 'Alice';                 WHERE name = 'Bob';
COMMIT;                               COMMIT;
```

Both transactions read `count = 2` from their own snapshot — neither sees the other's uncommitted update. Both conclude the invariant holds, each updates a *different* row, both commit. Result: **zero doctors on call.** No dirty read, no non-repeatable read, no phantom, no lost update on a single row — every classic anomaly check passes, yet the invariant is destroyed. That's write skew, and it slips straight through snapshot isolation / repeatable read.

### Fix 1: SELECT ... FOR UPDATE

Take explicit locks on the rows your decision depends on, forcing the transactions to serialize:

```sql
BEGIN;
SELECT * FROM doctors
  WHERE on_call = true AND shift_id = 1234
  FOR UPDATE;                 -- locks the matching rows
-- ...decide, then UPDATE...
COMMIT;
```

`FOR UPDATE` locks the rows the query returns, so the second transaction blocks until the first commits, then re-evaluates against the new state and correctly refuses. The caveat: it only protects when the conflict is over rows that **already exist**. If the write skew turns on the *absence* of rows (e.g. "no overlapping meeting-room booking exists, so insert one"), there are no rows to lock — that's a phantom-shaped conflict, and `FOR UPDATE` can't help.

### Fix 2: SERIALIZABLE

The general fix. In Postgres, `SET TRANSACTION ISOLATION LEVEL SERIALIZABLE` runs SSI: it tracks the read/write dependency between the two transactions (each read a set the other wrote) and aborts one at commit with a serialization failure. You retry the aborted transaction; on retry its snapshot sees `count = 1` and it correctly refuses. SSI covers the phantom-shaped cases too, because it uses predicate locking rather than locking concrete rows. The cost is optimistic: under contention you pay in aborts and must wrap every transaction in retry logic.

The trade-off in one line: `FOR UPDATE` is targeted, pessimistic, and needs you to know exactly which rows matter; `SERIALIZABLE` is general, optimistic, and needs a retry loop. Retry loops are the same discipline you already need for idempotent consumers — see the [delivery-semantics writeup](/articles/distributed-systems/2026-08-10-delivery-semantics-exactly-once) on why "just retry" only works when the operation is safe to repeat.

## The interview answer, compressed

Levels are defined by anomalies, not by their names. Read committed stops dirty reads; repeatable read adds non-repeatable reads; serializable stops everything including write skew. But the names are per-database fiction: Postgres has no dirty reads at all, its repeatable read is snapshot isolation that also blocks phantoms, and MySQL InnoDB defaults to repeatable read while Postgres defaults to read committed. MVCC makes weak isolation cheap — snapshots let readers and writers pass each other — but snapshot isolation still permits write skew, which you fix with `SELECT ... FOR UPDATE` on the rows your invariant reads, or with true `SERIALIZABLE` plus a retry loop.

**Try next:** open two `psql` sessions, `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ` in both, and reproduce the doctors write skew end to end — watch both commit and the invariant break. Then rerun at `SERIALIZABLE` and confirm one aborts with `could not serialize access`; wrap it in a retry and prove the invariant holds.

## SSI: serializable without serial execution

Postgres 9.1+ implements **Serializable Snapshot Isolation** (Cahill's algorithm, productionized by Ports & Grittner). It runs transactions on plain snapshots but additionally tracks reads with non-blocking **SIREAD locks** (including predicate/index-range granularity, which is how phantoms and write skew through predicates get caught). It watches for **rw-antidependencies** — T1 read something T2 later wrote. Theory says every SI anomaly requires two consecutive rw-antidependencies in the conflict graph; when SSI sees that "dangerous structure," it aborts one transaction with `40001`.

The contract this imposes on application code: *any* serializable transaction can be rejected even without touching the same rows as anyone else, so you must wrap them in a retry loop. False positives exist (the check is conservative), but there's no blocking and, per the VLDB paper, modest overhead versus plain SI. Compare that to the pessimistic alternative — 2PL takes shared locks on everything read and blocks instead of aborting.

Interview closer: "repeatable read in Postgres" and "repeatable read in MySQL/InnoDB" are different animals (InnoDB's uses next-key locking for writes and can still exhibit its own quirks), so always answer in terms of anomalies, not level names.

**Try next:** run the doctors demo in two psql terminals at REPEATABLE READ, then at SERIALIZABLE, and inspect the SIREAD locks mid-flight with `SELECT locktype, relation::regclass, mode FROM pg_locks WHERE mode = 'SIReadLock';`.
