---
title: 'Optimistic vs Pessimistic Concurrency Control: 2PL, OCC, and When Each Wins'
date: 2026-08-15
track: distributed-systems
summary: Pessimistic control (two-phase locking) assumes conflict and blocks up front; optimistic control (OCC) assumes no conflict and checks at commit, aborting the loser. The choice is a bet on contention level. Covers strict 2PL and its deadlocks, the read/validate/write phases of Kung & Robinson's 1981 OCC, where MVCC sits between them, and a sketch of OCC backward validation.
reading_time: 7
tags:
- concurrency-control
- two-phase-locking
- occ
- mvcc
- serializability
- 2pl
- compare-and-swap
- etag
- transactions
sources:
- title: Kung & Robinson, On Optimistic Methods for Concurrency Control (ACM TODS, 1981)
  url: https://dl.acm.org/doi/10.1145/319566.319567
- title: PostgreSQL docs — Introduction to Multiversion Concurrency Control (MVCC)
  url: https://www.postgresql.org/docs/current/mvcc-intro.html
- title: 'Databricks — Concurrency Control: locking, MVCC, and optimistic strategies'
  url: https://www.databricks.com/blog/concurrency-control
- title: Ziqi Wang — Analyzing Optimistic Concurrency Control Anomalies and Solutions
  url: https://wangziqi2013.github.io/article/2018/03/21/Analyzing-OCC-Anomalies-and-Solutions.html
- title: Kung & Robinson — On Optimistic Methods for Concurrency Control (ACM TODS, 1981)
  url: https://www.cs.cmu.edu/~dga/15-712/F07/lectures/12-optimism.pdf
- title: Optimistic concurrency control (Wikipedia)
  url: https://en.wikipedia.org/wiki/Optimistic_concurrency_control
- title: RFC 9110 §13.1.1 / §8.8 — If-Match, ETag, and conditional requests
  url: https://www.rfc-editor.org/rfc/rfc9110#name-if-match
- title: HTTP conditional requests (MDN)
  url: https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Conditional_requests
- title: Handling Optimistic Concurrency with ETags (Ed-Fi Alliance)
  url: https://docs.ed-fi.org/reference/data-exchange/api-guidelines/design-and-implementation-guidelines/api-implementation-guidelines/handling-optimistic-concurrency-with-etags/
---

**Gist.** Two transactions touching the same rows must be ordered somehow, and the ordering can be established either before the work or after it. **Pessimistic** control (two-phase locking) establishes it before, by making each transaction acquire locks ahead of every access; **optimistic** control (OCC) establishes it after, by letting both run against private copies and checking at commit whether the result is still serializable. The costs are complementary: pessimistic control pays in blocking and deadlock, optimistic control pays in work discarded at abort time.

This is a different axis from isolation *levels*. A system picks a control strategy first; the anomalies it forbids follow from how strictly that strategy is applied.

## Pessimistic: two-phase locking

The classic pessimistic protocol is **two-phase locking (2PL)**. Each transaction acquires locks as it needs them and, once it has released *any* lock, may never acquire another. That rule splits its life into a **growing phase** (acquisitions only) and a **shrinking phase** (releases only). The guarantee follows from the split: there exists a single instant — the **lock point**, the end of the growing phase — at which the transaction holds every lock it will ever hold. Ordering transactions by their lock points yields a serial schedule equivalent to the concurrent one.

Locks come in **modes**. A shared (S) lock permits other readers; an exclusive (X) lock excludes everyone. S is compatible with S, X with nothing. Reads take S, writes take X, and the compatibility matrix decides who waits.

Plain 2PL still permits a subtle failure. Because a write lock may be released during the shrinking phase but before commit, another transaction can read a value that is subsequently rolled back, and must then be rolled back itself — a **cascading abort**. **Strict 2PL** removes it by holding *all exclusive locks until commit or abort*; **strong strict 2PL (SS2PL)** holds *all* locks, shared and exclusive, until transaction end. SS2PL is the variant lock-based engines commonly implement, and it makes recovery straightforward: no committed transaction has ever observed uncommitted state.

The price of blocking is **deadlock**: T1 holds A and requests B, T2 holds B and requests A. Neither can release, because neither has entered its shrinking phase. Systems either *detect* deadlock by finding a cycle in the waits-for graph and aborting a victim, or *prevent* it with timestamp schemes — **wound-wait** and **wait-die** — that permit waiting only in one direction of timestamp order, so no cycle can form. Under high contention a lock-based system spends its time waiting or unwinding rather than committing.

## Optimistic: read, validate, write

Kung and Robinson's 1981 OCC takes the opposite bet. A transaction runs in three phases:

1. **Read phase** — execute normally, buffering all writes in a private workspace while recording a **read set** and a **write set**. The shared database is never modified.
2. **Validation phase** — atomically determine whether committing now preserves serializability with respect to transactions that committed during this transaction's read phase.
3. **Write phase** — on successful validation, flush the buffered writes; otherwise **abort and retry** from the beginning.

No locks are held during the read phase, which is the long one. Readers therefore never block writers, and **deadlock is structurally impossible** — a transaction that cannot be ordered fails validation instead of waiting. The cost is that every unit of work in a failed transaction is discarded, and a doomed transaction is not discovered until commit time.

**Backward validation**, the common form, checks the committing transaction against every transaction that finished after it started. Each committed transaction receives a **transaction number** in commit order; the numbers are the serialization order. If a transaction that committed during the read phase wrote into the validating transaction's read set, the validating transaction observed a stale snapshot and cannot be ordered after it, so it aborts.

Note what this does not catch when weakened. Write-skew-style hazards — two transactions reading overlapping data and writing disjoint rows — are only detected because **read sets** participate in validation. An implementation that compares write sets alone has known anomalies.

### Implementation sketch (Scala)

```scala
final case class Txn(
    startTn: Long,                  // highest tn committed when the read phase began
    readSet: Set[Key],
    writeSet: Map[Key, Value]
)

final case class Committed(finishTn: Long, writeSet: Map[Key, Value])

final class Occ:
  private var nextTn: Long = 0
  private var log: Vector[Committed] = Vector.empty
  private var db: Map[Key, Value] = Map.empty

  /** Validation and write phase are one critical section: the log must not
    * gain entries between the check and the assignment of finishTn. */
  def validateAndCommit(txn: Txn): Boolean = synchronized:
    val overlapping = log.iterator
      .filter(_.finishTn > txn.startTn)          // committed during our read phase
      .exists(c => c.writeSet.keySet.exists(txn.readSet))
    if overlapping then false                     // stale read: discard, retry whole txn
    else
      nextTn += 1
      db = db ++ txn.writeSet                     // write phase, atomic to observers
      log = log :+ Committed(nextTn, txn.writeSet)
      true

  def begin(): Long = synchronized(nextTn)
```

The sketch keeps `log` unbounded; a real implementation prunes entries whose `finishTn` precedes the oldest active `startTn`, since no live transaction can conflict with them.

## Where MVCC fits

**Multiversion concurrency control (MVCC)** is orthogonal to the optimistic/pessimistic axis: it retains *old versions* of rows so a reader sees a consistent snapshot without acquiring locks. The PostgreSQL documentation states the property directly — "reading data never blocks writing data and writing data never blocks reading data." MVCC removes read-write conflicts but still requires a strategy for **write-write** conflicts. PostgreSQL pairs MVCC with row locks for updates, a pessimistic element, while its `SERIALIZABLE` mode adds optimistic serializable snapshot isolation (SSI) checks that abort on dangerous read-write dependency structures.

| | Pessimistic (SS2PL) | Optimistic (OCC) | MVCC |
|---|---|---|---|
| Core assumption | conflicts are common | conflicts are rare | readers should not block |
| Conflict handling | block until lock free | abort and retry at commit | version per writer |
| Readers vs writers | block each other | do not interfere | never block |
| Failure mode | deadlock, lock waits | wasted work, starvation | version bloat / vacuum |
| Wasted work on conflict | little (blocks, then proceeds) | whole transaction | little |
| Best when | high contention, long txns | low contention, short txns | read-heavy workloads |
| Examples | DB2, MySQL (locking paths) | in-memory OLTP (Silo, Hekaton) | Postgres, Oracle, CockroachDB |

## When each wins

The deciding variable is **contention**. Under low contention most validations pass, so OCC delivers lock-free reads with no deadlock-detection machinery, which is why modern in-memory OLTP engines lean optimistic. Under high contention OCC degrades: transactions reach commit only to abort, and **a long transaction can starve indefinitely** while shorter ones repeatedly validate ahead of it. Pessimistic 2PL is better placed here because a blocked transaction *resumes* rather than restarts — it pays the wait once instead of redoing its work on every retry. Long-running transactions favour locking for the same reason: OCC's abort penalty scales with the amount of work lost.

No production system ships a pure version of either. Databases layer MVCC for reads with locks or optimistic validation for writes, so the question "is this database optimistic or pessimistic?" generally answers *both, in different places*.

## The same bet at the API layer: ETag and If-Match

The pattern is not database-specific. HTTP provides OCC across stateless clients through conditional requests (RFC 9110). A `GET` returns an `ETag`, an opaque version token for the resource. The client echoes it on write in `If-Match`; the server applies the change only if the current entity tag still matches, and otherwise returns **412 Precondition Failed**.

```http
GET /accounts/42            -> 200  ETag: "v7"
PUT /accounts/42            If-Match: "v7"
   -> 200 (ETag now "v8")   if unchanged
   -> 412 Precondition Failed   if another writer already moved it to "v8"
```

A 412 is the HTTP equivalent of `rows affected = 0`: re-fetch, reconcile, retry. The three phases are the same, stretched across a network with no server-side lock held between read and write — which is what makes it viable for lost-update prevention on public APIs, where a lock held across client think-time would be held for an unbounded interval.

## Pitfalls

- **Releasing a write lock before commit under plain 2PL admits cascading aborts**: a reader that observed the uncommitted value must be rolled back too. Strict 2PL, which holds exclusive locks to end of transaction, is the fix.
- **Acquiring locks on the same rows in different orders across code paths produces deadlock cycles**, surfacing in PostgreSQL as SQLSTATE `40P01`. A consistent global lock ordering removes the cycle.
- **Validating write sets only leaves write-skew undetected**: two transactions read overlapping rows, write disjoint rows, and both commit into a state no serial order produces. Read sets must participate in validation.
- **A long transaction under OCC can starve**: it is repeatedly invalidated by shorter transactions that commit inside its read phase, and it never observes a stable enough snapshot to validate.
- **Retrying an aborted OCC transaction without backoff amplifies contention**, because the retry re-enters the same conflict window that caused the abort.
- **MVCC's cost is version retention, not blocking**: old row versions accumulate until vacuum reclaims them, so a long-lived read transaction pins versions and inflates table size while never itself blocking.
- **Treating an ETag as a content hash breaks the protocol contract**: it is an opaque token, and a client that constructs or compares it structurally rather than echoing it verbatim in `If-Match` can send a precondition the server cannot honour.
