---
title: "Optimistic vs Pessimistic Concurrency Control: 2PL, OCC, and When Each Wins"
date: 2026-08-15
track: distributed-systems
summary: "Pessimistic control (two-phase locking) assumes conflict and blocks up front; optimistic control (OCC) assumes no conflict and checks at commit, aborting the loser. The right choice is a bet on your contention level. Here's strict 2PL and its deadlocks, the read/validate/write phases of Kung & Robinson's 1981 OCC, where MVCC sits between them, and a pseudocode of OCC backward validation."
reading_time: 6
tags: [concurrency-control, two-phase-locking, occ, mvcc, serializability]
sources:
  - title: "Kung & Robinson, On Optimistic Methods for Concurrency Control (ACM TODS, 1981)"
    url: "https://dl.acm.org/doi/10.1145/319566.319567"
  - title: "PostgreSQL docs — Introduction to Multiversion Concurrency Control (MVCC)"
    url: "https://www.postgresql.org/docs/current/mvcc-intro.html"
  - title: "Databricks — Concurrency Control: locking, MVCC, and optimistic strategies"
    url: "https://www.databricks.com/blog/concurrency-control"
  - title: "Ziqi Wang — Analyzing Optimistic Concurrency Control Anomalies and Solutions"
    url: "https://wangziqi2013.github.io/article/2018/03/21/Analyzing-OCC-Anomalies-and-Solutions.html"
---

Two transactions want the same rows. You can assume they will collide and make each one grab locks before it touches anything — **pessimistic** control — or you can assume collisions are rare, let both run full speed against private copies, and check for a conflict only at commit — **optimistic** control. That single bet drives everything downstream: pessimistic control pays with blocking and deadlocks, optimistic control pays with wasted work and aborts. This is a different axis from isolation *levels*; a system picks a control strategy first, then the anomalies it forbids fall out of how strictly that strategy is applied.

## Pessimistic: two-phase locking

The classic pessimistic protocol is **two-phase locking (2PL)**. Each transaction acquires locks as it needs them and, once it has released *any* lock, may never acquire another. That splits its life into a **growing phase** (only acquires) and a **shrinking phase** (only releases). The rule is what guarantees a serializable schedule: it forces a single point where the transaction holds all the locks it will ever hold, and that point can be ordered against every other transaction.

Locks come in **modes**. A shared (S) lock permits other readers; an exclusive (X) lock blocks everyone. S is compatible with S, X with nothing. Reads take S, writes take X, and the compatibility matrix decides who waits.

Plain 2PL still allows a subtle problem: it can release a write lock before commit, so another transaction reads a value that then gets rolled back — a **cascading abort**. **Strict 2PL** fixes this by holding *all exclusive locks until commit or abort*; **strong strict 2PL (SS2PL)** holds *all* locks, shared and exclusive, until the end. SS2PL is what most textbook "locking" databases actually implement because it makes recovery clean.

The cost of blocking is **deadlock**: T1 holds A and wants B, T2 holds B and wants A. Nobody releases (they can't — shrinking hasn't started). Systems either *detect* deadlocks by finding a cycle in the waits-for graph and aborting a victim, or *prevent* them with timestamp schemes like wound-wait and wait-die that never let a cycle form. Either way, under high contention a lock-based system spends its time waiting or unwinding, not committing.

## Optimistic: read, validate, write

Kung & Robinson's 1981 OCC takes the opposite bet. A transaction runs in three phases:

1. **Read phase** — execute normally, but buffer all writes in a private workspace and record a **read set** and **write set**. The shared database is never touched.
2. **Validation phase** — atomically check whether committing now would preserve serializability against transactions that committed during our read phase.
3. **Write phase** — if validation passes, flush the buffered writes to the database; otherwise **abort and retry** from scratch.

No locks are held during the (long) read phase, so readers never block writers and there are no deadlocks — a stuck transaction just fails validation. The price is that all the work in a failed transaction is thrown away, and a doomed transaction isn't discovered until commit time.

**Backward validation** — the common form — checks the committing transaction against everyone who finished after it started:

```python
def validate_and_commit(txn, committed_log, next_tn):
    # txn.start_tn = highest transaction number that had committed
    #                when txn began its read phase
    for other in committed_log:
        if other.finish_tn > txn.start_tn:        # committed during our read phase
            if txn.read_set & other.write_set:    # we read something they overwrote
                abort(txn)                         # stale read -> retry whole txn
                return
    txn.finish_tn = next_tn()      # assign serialization number, in commit order
    apply(txn.write_set)           # write phase: made visible atomically
    committed_log.append(txn)
```

The serializability argument is exactly the paper's condition: if some concurrently-committed transaction wrote into our read set, our snapshot is stale and we can't be ordered after it, so we abort. Note what OCC does *not* catch for free — the write-skew-style hazards where two transactions read overlapping data and write disjoint rows still require validating read sets, not just write sets, which is why naive "check only write-write conflicts" OCC has known anomalies.

## Where MVCC fits

**Multiversion concurrency control (MVCC)** is a third thing, orthogonal to the optimistic/pessimistic axis: it keeps *old versions* of rows so a reader sees a consistent snapshot without locking. PostgreSQL states it plainly — the main advantage is that "reading never blocks writing and writing never blocks reading." MVCC eliminates read-write conflicts, but it still needs a strategy for **write-write** conflicts: Postgres pairs MVCC with row locks (a pessimistic touch) for updates, while its `SERIALIZABLE` mode adds optimistic SSI checks that abort on dangerous read-write dependencies. So real systems mix all three.

| | Pessimistic (SS2PL) | Optimistic (OCC) | MVCC |
|---|---|---|---|
| Core assumption | conflicts are common | conflicts are rare | readers shouldn't block |
| Conflict handling | block until lock free | abort & retry at commit | version per writer |
| Readers vs writers | block each other | don't interfere | never block |
| Failure mode | deadlock, lock waits | wasted work, starvation | version bloat / vacuum |
| Wasted work on conflict | little (blocks, then proceeds) | whole transaction | little |
| Best when | high contention, long txns | low contention, short txns | read-heavy workloads |
| Examples | DB2, MySQL (locking paths) | in-memory OLTP (Silo, Hekaton) | Postgres, Oracle, CockroachDB |

## When each wins

The deciding variable is **contention**. Under low contention, most OCC validations pass, so you get lock-free reads and no deadlock-detection overhead — this is why modern in-memory OLTP engines lean optimistic. Under high contention, OCC degrades badly: transactions repeatedly reach commit only to abort, and the same hot transaction can starve while cheaper ones keep beating it. Pessimistic 2PL wins here because a blocked transaction resumes rather than restarts — it pays the wait *once* instead of redoing work on every retry. Long-running transactions also favor locking, since OCC's abort penalty scales with how much work is lost.

The honest caveat: nobody ships a pure version of either. Production databases layer MVCC for reads, locks or optimistic validation for writes, and tune the mix to the workload — so "is this database optimistic or pessimistic?" almost always answers *both, in different places*.

**Try next:** open two `psql` sessions and force a deadlock (each updates two rows in opposite order) to watch Postgres detect the cycle and abort a victim with SQLSTATE `40P01`; then rerun at `SERIALIZABLE` and trigger an optimistic `40001` abort instead — same conflict, opposite strategy.
