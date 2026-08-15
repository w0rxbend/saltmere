---
title: "Distributed locking: a lease is not mutual exclusion without a fencing token"
date: 2026-08-11
track: sys-patterns
summary: "A distributed lock separates workers on the happy path, but a process pause can outlive the lease and let a zombie holder write anyway. The remedy is not a stronger lock protocol: it is a monotonically increasing fencing token that the protected resource itself checks."
reading_time: 7
tags: [distributed-locking, fencing-tokens, redis, redlock, etcd]
sources:
  - title: "Kleppmann — How to do distributed locking"
    url: "https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html"
  - title: "antirez — Is Redlock safe?"
    url: "https://antirez.com/news/101"
  - title: "Redis — Distributed Locks with Redis (Redlock)"
    url: "https://redis.io/docs/latest/develop/use/patterns/distributed-locks/"
  - title: "Apache ZooKeeper — Recipes and Solutions (Locks)"
    url: "https://zookeeper.apache.org/doc/r3.8.5/recipes.html"
  - title: "etcd clientv3/concurrency — Session & Mutex"
    url: "https://pkg.go.dev/go.etcd.io/etcd/client/v3/concurrency"
---

**Gist.** Several workers contend for a task that must run one at a time — draining a queue, reconciling state, writing to shared storage — and the usual answer is a distributed lock with an expiry, that is, a lease. A lease guarantees that at most one client *believes* it holds the lock at any instant only if no client can be paused past its own expiry, which no asynchronous system guarantees; the surviving guarantee comes from a **fencing token**, a number that increases on every grant and that the protected resource compares against the highest token it has already accepted. The cost is that the resource must become a participant in the locking protocol: it needs durable per-resource token state, updated atomically with the data it protects.

## The classic lease, three ways

The single-node Redis recipe is `SET resource_key random_value NX PX 30000`: create the key only when absent (`NX`), with a 30-second expiry (`PX`) so a dead holder does not hold the lock indefinitely. The `random_value` guards *release*: a Lua script deletes the key only when its value still matches the releasing client's value, so a client that lost the lock by expiry cannot delete a successor's lock. To tolerate the failure of one node, [Redlock](https://redis.io/docs/latest/develop/use/patterns/distributed-locks/) performs the same acquisition against N independent masters and reports success only when a majority acknowledge within the validity window.

The consensus-store variants differ in mechanism and agree in shape. In [ZooKeeper](https://zookeeper.apache.org/doc/r3.8.5/recipes.html) each contender creates an **ephemeral sequential** znode beneath a lock path; the holder is the contender with the lowest sequence number, and the *ephemeral* flag removes the znode when the client's session ends, releasing the lock on crash without operator action. [etcd](https://pkg.go.dev/go.etcd.io/etcd/client/v3/concurrency) expresses the same recipe as a lease-backed `Session` with a `Mutex` built on it. **All three are leases — locks carrying a deadline that must be renewed — and the deadline is what creates the hazard below.**

## Where the lease stops covering the write

The hazard Martin Kleppmann [described in 2016](https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html) is independent of any Redis defect. Client 1 acquires the lock and then stalls: a stop-the-world garbage-collection pause, a hypervisor freeze, a paging storm, a delayed network. During the stall the lease expires. The lock service behaves correctly and grants the lock to Client 2, which performs its work. Client 1 then resumes with no evidence that time has passed, still believing it holds the lock, and issues its write.

Two clients have written to the resource, and neither observes a conflict.

> "If the GC pause lasts longer than the lease expiry period, and the client doesn't realise that it has expired, it may go ahead and make some unsafe change."

Re-checking "am I still the holder?" immediately before the write does not repair this: **the pause can occur between the check and the write**, so the check's answer is stale by the time it is acted on. Tightening the lock protocol cannot close the gap, because the failure occurs on the client side of the wire, after the lock service has already done everything correct available to it. This is the zombie-holder problem also discussed in the [leader-election pattern](/articles/sys-patterns/2026-07-25-leader-election-pattern), and the reason that electing a single leader — by lease or by a [bully or ring election](/articles/distributed-systems/2026-07-30-election-algorithms-bully-ring) — is not by itself sufficient.

## The Kleppmann–antirez exchange

Kleppmann's second critique concerns assumptions rather than pauses: Redlock's safety rests on **bounded clock drift, bounded process pauses, and bounded network delay**. An asynchronous system supplies none of those bounds, so a lock intended for correctness should not depend on them.

Salvatore Sanfilippo (antirez) [replied](https://antirez.com/news/101) that Redlock requires each process to "count 5 seconds with a maximum of 10% error" — a weaker requirement than synchronised wall clocks — and that measuring elapsed time before and after acquisition makes the algorithm tolerant of unbounded message delay. He also observes a limit of fencing tokens themselves: **the order in which clients acquire the lock need not match the order in which they reach the resource**, so a token is only useful when the resource is the component enforcing it. The two positions answer different questions. Redlock is not a correctness lock under adversarial timing, and for systems where the timing assumptions hold the additional machinery has a cost. The reconciliation is fencing rather than a verdict on Redlock.

## The fencing token

A **fencing token** is a number issued by the lock service that increases on every grant. The client stamps each write with its token. The protected resource retains the highest token it has ever accepted and **rejects any write carrying a token less than or equal to it**. The zombie's write carries a smaller token and is refused by the storage layer regardless of how stale the client's belief is.

The load-bearing predicate is `token <= lastSeen ⇒ reject`. Safety resides **in the resource**, the only participant present at the instant of the write. The counter need not be built from scratch: etcd exposes a global, monotonically increasing **revision** on every mutation, and ZooKeeper exposes the **zxid** together with the znode's sequence number and `cversion`. Redis has no monotonic counter shared across Redlock's independent masters, which is the gap Kleppmann identified; an `INCR` layered on one node reintroduces that node as a single point.

### Implementation sketch (Scala)

```scala
final case class StaleToken(seen: Long, offered: Long)
    extends RuntimeException(s"rejecting $offered, already accepted $seen")

/** State the resource keeps per protected key: highest token accepted, plus the
  * payload written under it. Both fields must move in one atomic step. */
final case class Guarded[A](lastSeen: Long, value: A)

final class FencedResource[A](initial: A):
  private val state = java.util.concurrent.atomic.AtomicReference(Guarded(0L, initial))

  /** Returns the accepted state, or fails when the writer's grant is superseded. */
  def write(token: Long, payload: A): Either[StaleToken, Guarded[A]] =
    // getAndUpdate returns the state the successful attempt observed, so the
    // strict `>` guard can be re-decided on it; the function may be retried
    // under contention and therefore stays pure.
    val prev = state.getAndUpdate: cur =>
      if token > cur.lastSeen then Guarded(token, payload) else cur
    if token > prev.lastSeen then Right(Guarded(token, payload))
    else Left(StaleToken(prev.lastSeen, token))

  def read: Guarded[A] = state.get
```

The `AtomicReference` stands in for whatever makes the pair durable in a real resource — a compare-and-set on an object store, a row update guarded by a `WHERE last_seen < ?` predicate, a transaction. **Storing the token in a separate record from the data reopens the hazard**, because the write can be interleaved between the two updates.

## Efficiency locks and correctness locks

The consequential question is what follows from the lock failing.

| | Efficiency lock | Correctness lock |
|---|---|---|
| Purpose | Avoid redundant work | Prevent data corruption |
| If two hold it | Wasteful, harmless (duplicate email) | Damaging (double charge, corrupt file) |
| Redis or Redlock alone | Adequate | **Inadequate** |
| Needs fencing? | No | **Yes** |

Where duplicate execution is merely wasteful — one duplicate notification, one recomputed cache entry — a plain lease suffices and fencing adds cost without changing the outcome. Where a second writer corrupts a ledger, a fencing token enforced at the resource is required. The failure mode most often observed is an efficiency lock deployed where correctness was the requirement.

An empirical check: acquire a lock through etcd's `clientv3/concurrency` and record the returned `Revision` as the token. Suspend the holder with `SIGSTOP` past the lease time-to-live (TTL), let a second client acquire at a higher revision and write, then resume the first process and confirm that its lower-revision write is refused by the `token <= lastSeen` guard.

## Pitfalls

- **Verifying lock ownership immediately before the write.** The check succeeds, the process is descheduled, the lease expires, and the write lands after another client's — the interval between check and write is unprotected by construction.
- **Storing the fencing token outside the transaction that writes the data.** A crash or interleaving between the two updates leaves a token that does not describe the stored value, and a stale writer passes the guard.
- **Using `>=` instead of `>` when comparing tokens.** A retried write from a superseded holder reusing its old token is accepted, which is the case the token exists to reject.
- **Deleting a Redis lock key without comparing the stored random value.** A client whose lease already expired deletes the successor's lock, and two clients proceed concurrently.
- **Treating a lock-acquisition sequence number as an ordering of writes.** Acquisition order need not match arrival order at the resource, so a token constrains only what the resource itself compares.
- **Deriving tokens from a single `INCR` beside a Redlock quorum.** The counter's availability and durability are not those of the quorum, so the token source fails independently of the lock.
- **Assuming an ephemeral znode or etcd lease disappears the instant the client dies.** Removal follows session expiry, so the window in which the old holder can still act is the session timeout, not zero.
