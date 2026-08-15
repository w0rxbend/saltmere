---
title: 'Isolation levels & MVCC: the anomalies, not the names, are the spec'
date: 2026-08-10
track: distributed-systems
summary: 'Isolation levels are defined by the read phenomena they forbid — dirty read, non-repeatable read, phantom — but the SQL standard''s names are not portable: PostgreSQL has no true read-uncommitted, its repeatable read is snapshot isolation, and MySQL InnoDB defaults to repeatable read. Multi-version concurrency control lets readers avoid blocking writers, yet snapshot isolation still permits write skew and lost update. Covers the anomaly/level matrix, a concrete write-skew case, and the SERIALIZABLE and SELECT FOR UPDATE remedies.'
reading_time: 7
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

**Gist.** Concurrent transactions interleave, and the interleaving can produce results no serial execution would produce. Isolation levels are not a quality scale but a specification: each level is defined by the set of concurrency anomalies it forbids, and multi-version concurrency control (MVCC) implements the weaker levels by letting each transaction read from a consistent snapshot of committed versions instead of blocking on writers. The cost is that snapshot isolation — the level most engines call *repeatable read* — still admits write skew and lost update, so preserving an invariant that spans multiple rows requires either explicit row locks or a serializable level whose transactions can abort and must be retried.

## The read phenomena

The ANSI SQL standard names three phenomena. They are the vocabulary of the specification.

- **Dirty read** — a transaction reads a row another transaction has written but not committed. If the writer rolls back, the value read never existed in any committed state.
- **Non-repeatable read** — a transaction reads a row, a second transaction commits an *update* to it, and a re-read within the *same* transaction returns a different value.
- **Phantom read** — a transaction runs a predicate query (`WHERE status = 'pending'`), a second transaction *inserts* a matching row and commits, and re-running the query returns an additional row. A non-repeatable read concerns a row's *value*; a phantom concerns the *set of matching rows*.

Two further phenomena the ANSI list omits are named by Berenson et al. (as P4 *lost update* and A5B *write skew*) and treated at length in DDIA Ch. 7. Both survive levels whose names suggest safety:

- **Lost update** — two transactions read a value, each modifies it based on what it read, and both write back. The second write overwrites the first, and the first update is lost. The read-modify-write counter increment is the canonical instance.
- **Write skew** — the generalization of lost update. Two transactions read an overlapping set of rows, each decides based on what it read, and each writes to a *different* row. Each write is individually legal; jointly they violate an invariant that spanned both reads.

## The four levels, by what they forbid

Each level forbids everything the weaker levels forbid, plus one further phenomenon.

| Isolation level | Dirty read | Non-repeatable read | Phantom read | Serialization anomaly (write skew) |
| --- | --- | --- | --- | --- |
| Read uncommitted | Allowed | Allowed | Allowed | Allowed |
| Read committed | Prevented | Allowed | Allowed | Allowed |
| Repeatable read | Prevented | Prevented | Allowed* | Allowed |
| Serializable | Prevented | Prevented | Prevented | Prevented |

The asterisk is the point at which the standard and real engines part company.

## The names are not portable

Berenson et al., *A Critique of ANSI SQL Isolation Levels* (SIGMOD 1995), argues that the standard defines levels through prose descriptions of phenomena that are ambiguous, and that the phenomena as written do not characterise snapshot isolation. Implementations diverged accordingly, so a level name alone does not determine which anomalies are possible.

**PostgreSQL** exposes four level names but implements three distinct behaviours.

- There is **no true read-uncommitted**: a request for it behaves as read committed. Under MVCC an uncommitted version is not visible to any other transaction, so a dirty read cannot occur at *any* level.
- Its **repeatable read is snapshot isolation**, stronger than the standard requires. The documentation states that this level prevents all the described phenomena except serialization anomalies, and that it **does not allow phantom reads** — the asterisk in the matrix. A stronger guarantee is conforming, because the standard constrains which anomalies must *not* occur, not which must.
- **Serializable** (since 9.1) is implemented by Serializable Snapshot Isolation (SSI): snapshot isolation plus runtime tracking of read/write dependencies, aborting a transaction with `could not serialize access due to read/write dependencies` when the observed interleaving has no equivalent serial order.

**MySQL InnoDB** defaults to **repeatable read**, whereas PostgreSQL defaults to read committed — a behavioural difference that appears on migration. InnoDB serves plain `SELECT` from consistent-read snapshots, but for locking reads it applies **next-key locks** (a row lock combined with a gap lock on the space between index records), which prevents phantoms for those statements. "Repeatable read" therefore denotes materially different behaviour in the two engines, and the accurate formulation is in terms of anomalies: the standard permits phantoms at repeatable read; PostgreSQL's snapshot-isolation implementation forbids them; InnoDB blocks them for locking reads via next-key locks.

## MVCC: the visibility rule

Rather than overwriting a row in place, MVCC retains **multiple committed versions**, each tagged with the transaction that created it. A write appends a new version; the prior version remains visible to transactions that began earlier.

At the start of a transaction — or at its first read, depending on level — the engine captures a **snapshot**: the set of transaction identifiers committed as of that instant. Every read then filters the version chain to the latest version committed before the snapshot and not created by an in-flight transaction. The consequence is that **a reader never waits for a writer and a writer never waits for a reader**, because they address different versions of the same row; a long analytical scan can proceed against a live transactional database without blocking writes.

The level determines snapshot lifetime. Read committed takes a **fresh snapshot per statement**, which is why a re-read can observe a newer value. Snapshot isolation takes **one snapshot for the whole transaction**, which is precisely what makes reads repeatable.

## Where snapshot isolation fails: write skew

Snapshot isolation is not serializable. DDIA's on-call doctors example is the standard counterexample. The invariant: at least one doctor must remain on call per shift. Two doctors, both on call, concurrently request to go off call.

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

Each transaction reads `count = 2` from its own snapshot and does not observe the other's uncommitted update. Each concludes the invariant holds, each updates a *different* row, and both commit. The resulting state has **zero doctors on call**. No dirty read, no non-repeatable read, no phantom and no single-row lost update occurred: every classic anomaly check passes while the invariant is destroyed. **The invariant spans rows that no single write touches, which is exactly the case snapshot isolation does not cover.**

### Fix 1: SELECT ... FOR UPDATE

Explicit locks on the rows the decision depends on force the transactions to serialize.

```sql
BEGIN;
SELECT * FROM doctors
  WHERE on_call = true AND shift_id = 1234
  FOR UPDATE;                 -- locks the matching rows
-- ...decide, then UPDATE...
COMMIT;
```

`FOR UPDATE` locks the rows the query returns, so the second transaction blocks until the first commits. What happens next depends on the level: at read committed PostgreSQL re-evaluates the locking clause against the newly committed row version, so the second transaction sees `count = 1` and correctly refuses; at repeatable read it instead aborts with a serialization failure, because the updated row is outside its snapshot. Either way the invariant survives, but the repeatable-read case still needs a retry. The limitation is structural: **only rows that already exist can be locked.** Where the invariant turns on the *absence* of rows — "no overlapping booking exists, therefore insert one" — the conflict is phantom-shaped and there is nothing for `FOR UPDATE` to lock.

### Fix 2: SERIALIZABLE

In PostgreSQL, `SET TRANSACTION ISOLATION LEVEL SERIALIZABLE` runs SSI. It records the read/write dependency between the two transactions — each read a set the other subsequently wrote — and aborts one with a serialization failure. On retry the aborted transaction's new snapshot reads `count = 1` and refuses. SSI covers the phantom-shaped cases because it locks predicates rather than concrete rows. The mechanism is optimistic, so contention is paid in aborts, and every transaction requires retry logic.

The trade-off: `FOR UPDATE` is targeted and pessimistic and requires knowing in advance which rows carry the invariant; `SERIALIZABLE` is general and optimistic and requires a retry loop. The retry discipline matches that of idempotent consumers — see the [delivery-semantics writeup](/articles/distributed-systems/2026-08-10-delivery-semantics-exactly-once) on why retrying is safe only when the operation tolerates repetition.

## SSI: serializability without serial execution

PostgreSQL 9.1 and later implement Serializable Snapshot Isolation (Cahill's algorithm, productionized by Ports & Grittner, VLDB 2012). Transactions execute on ordinary snapshots, and reads are additionally tracked by non-blocking **SIREAD locks** — predicate locks recorded at tuple, page or relation granularity, and coarsened upward when too many accumulate. Because they cover ranges rather than only the rows a transaction wrote, phantom-shaped write skew is detected too. The engine watches for **rw-antidependencies**: T1 read something T2 subsequently wrote. The underlying theorem is that every cycle in the dependency graph of a snapshot-isolation execution contains two rw-antidependencies in sequence; on detecting that dangerous structure, SSI aborts one transaction with SQLSTATE `40001`.

The obligation this places on application code follows directly: **a serializable transaction can be rejected without having written any row another transaction touched**, so every such transaction must be wrapped in a retry loop. The check is conservative, so false positives occur; the VLDB paper reports modest overhead relative to plain snapshot isolation, and no read blocks. The pessimistic alternative, two-phase locking, takes shared locks on everything read and blocks rather than aborting.

### Implementation sketch (Scala)

The retry loop is the load-bearing part of a serializable client: detect SQLSTATE `40001` and re-execute the *entire* transaction body, since the aborted snapshot is unusable.

```scala
import java.sql.{Connection, SQLException}

final case class SerializationFailure(attempts: Int) extends Exception

/** Runs `body` at SERIALIZABLE, retrying on SQLSTATE 40001. `body` must be
  * re-executable: nothing outside the transaction may have been committed. */
def inSerializable[A](conn: Connection, maxAttempts: Int = 5)(body: Connection => A): A =
  def attempt(n: Int): A =
    conn.setTransactionIsolation(Connection.TRANSACTION_SERIALIZABLE)
    conn.setAutoCommit(false)
    try
      val a = body(conn)
      conn.commit()
      a
    catch
      // 40001 may surface at commit, not only at statement execution.
      case e: SQLException if e.getSQLState == "40001" =>
        conn.rollback()
        if n >= maxAttempts then throw SerializationFailure(n)
        Thread.sleep((1L << n) * 10)  // backoff: contention is the cause
        attempt(n + 1)
      case e: Throwable =>
        conn.rollback(); throw e
  attempt(1)

// The doctors invariant needs no explicit locking under SSI:
inSerializable(conn) { c =>
  val ps = c.prepareStatement(
    "SELECT count(*) FROM doctors WHERE on_call = true AND shift_id = ?")
  ps.setInt(1, 1234)
  val onCall = { val r = ps.executeQuery(); r.next(); r.getInt(1) }
  if onCall > 1 then
    val u = c.prepareStatement("UPDATE doctors SET on_call = false WHERE name = ?")
    u.setString(1, "Alice"); u.executeUpdate()
}
```

## Pitfalls

- **Assuming a level name transfers between engines.** A schema tested on PostgreSQL repeatable read (snapshot isolation, phantom-free) can exhibit phantoms elsewhere, because the standard permits them at that level.
- **Inheriting the default on migration.** PostgreSQL defaults to read committed and MySQL InnoDB to repeatable read; code moved between them silently changes snapshot lifetime — per statement versus per transaction — and re-reads that were stable start varying, or vice versa.
- **Treating repeatable read as sufficient for a cross-row invariant.** The doctors case commits both transactions without any of the three ANSI phenomena occurring; the invariant breaks with no error raised anywhere.
- **Using `SELECT ... FOR UPDATE` against an absence.** When the invariant depends on no matching row existing, the query returns no rows, locks nothing, and both transactions insert.
- **Omitting the retry loop at SERIALIZABLE.** SSI aborts are not exceptional under contention, and an unhandled `40001` surfaces as a user-visible failure on a transaction that would succeed on re-execution.
- **Retrying a transaction whose effects escaped it.** If the transaction body already sent an email or published a message, re-execution repeats that effect; only work confined to the transaction is safe to replay.
- **Reading `count(*)` and writing a different row.** This shape — decision from an aggregate, write to one member — is write skew by construction and passes every single-row conflict check.
