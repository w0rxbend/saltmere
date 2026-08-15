---
title: "Leases: time-bound ownership as a coordination primitive"
date: 2026-08-13
track: distributed-systems
summary: "A lease grants ownership for a bounded term: the holder renews it, and a crashed holder's grant lapses without any reconciliation protocol. From Gray & Cheriton's 1989 paper through Chubby's master and session leases to etcd time-to-live (TTL) objects and Raft leader leases that serve reads without a network round trip."
reading_time: 6
tags: [leases, coordination, etcd, chubby, raft]
sources:
  - title: "Leases: An Efficient Fault-Tolerant Mechanism for Distributed File Cache Consistency — Gray & Cheriton (SOSP 1989)"
    url: "https://dl.acm.org/doi/10.1145/74850.74870"
  - title: "The Chubby Lock Service for Loosely-Coupled Distributed Systems — Burrows (OSDI 2006)"
    url: "https://www.usenix.org/legacy/events/osdi06/tech/full_papers/burrows/burrows.pdf"
  - title: "How to create a lease (etcd v3.6 tutorial)"
    url: "https://etcd.io/docs/v3.6/tutorials/how-to-create-lease/"
  - title: "Low Latency Reads in Geo-Distributed SQL with Raft Leader Leases (YugabyteDB)"
    url: "https://www.yugabyte.com/blog/low-latency-reads-in-geo-distributed-sql-with-raft-leader-leases/"
  - title: "LeaseGuard: Raft Leases Done Right (PACMMOD/SIGMOD 2026)"
    url: "https://arxiv.org/abs/2512.15659"
---

**Gist.** Every "who owns this right now?" problem — cache validity, leader election, work-queue assignment — shares one failure mode: the owner dies while holding a grant that never lapses. A *lease* bounds the grant in time, making it renewable by the holder and reclaimable by everyone else once the term ends, so recovery needs no failure detector and no reconciliation protocol. The cost is that correctness is transferred onto the clock: the term must be sized against a drift bound, and every process pause between checking the lease and acting on it is a window in which two parties believe they own the same thing.

This article treats leases as renewable time-based ownership. The enforcement side — fencing tokens and the Redlock debate — is covered in [distributed locking done right](/articles/sys-patterns/2026-08-11-distributed-locking-fencing-tokens).

## The original mechanism: Gray & Cheriton, 1989

The lease paper addresses file cache consistency, but the mechanism generalises. A server grants a client a lease over some data for term *T*. While the lease is valid, the client may serve cached reads locally, and **the server promises to contact the holder before accepting a conflicting write**. That promise is what makes the local read safe.

The design's distinguishing property is its behaviour on failure: **nothing happens**. If the client crashes, the server waits out the remaining term and proceeds. If the network partitions, the same. Time is the recovery mechanism, so the protocol contains no failure detector to tune and no state to reconcile afterwards.

The term is the tuning knob, and the trade is symmetric. Short terms shorten the interval a writer must wait after a holder vanishes, at the price of renewal traffic proportional to 1/*T*. Long terms amortise renewals and lengthen that wait. Crucially, the correctness argument requires only a **bound on clock drift rate** — both sides measure the same duration on their own clocks — and never synchronised wall clocks.

## Chubby: leases at two levels

Google's Chubby lock service (Burrows, OSDI 2006) applies the primitive twice. The elected master holds a **master lease**: the replicas undertake not to elect another master while it lasts, and the master extends it by winning quorum votes. Clients therefore address one node without a consensus round per request.

Clients hold **session leases**, extended by KeepAlive remote procedure calls, with a default duration of 12 s. Every lock and every cached file handle a client holds is scoped to its session, so session expiry invalidates the whole set at once rather than key by key. When a master fails, clients enter a *grace period* of about 45 s during which the session is in jeopardy rather than dead, giving a newly elected master time to take over and honour existing sessions.

## etcd: the primitive exposed directly

etcd represents a lease as an object with a time-to-live (TTL). Keys are attached to it, and **when keepalives stop, every attached key is removed atomically**. etcd's own lock and leader-election APIs are built on this behaviour.

```console
$ etcdctl lease grant 60
lease 694d77aa9e38260f granted with TTL(60s)
$ etcdctl put /leader/api node-42 --lease=694d77aa9e38260f
$ etcdctl lease keep-alive 694d77aa9e38260f    # blocks, renews periodically
$ etcdctl lease timetolive 694d77aa9e38260f --keys
```

The equivalent flow in Go with `clientv3`; the client library renews in the background at roughly TTL/3 intervals:

```go
cli, _ := clientv3.New(clientv3.Config{Endpoints: []string{"localhost:2379"}})
lease, _ := cli.Grant(ctx, 10) // 10 s TTL
cli.Put(ctx, "/leader/api", "node-42", clientv3.WithLease(lease.ID))
ch, _ := cli.KeepAlive(ctx, lease.ID) // background renewal channel
// if this process stalls or dies, /leader/api disappears within 10 s
```

Stopping the keepalive removes the key once the TTL elapses, which is cleanup without a janitor process.

## Leader leases: reads without a round trip

The subtler use of a lease is as a *read optimisation* in Raft-family systems. A Raft leader cannot answer a read from local state unconditionally, because it may have been deposed without yet learning so; the result would be a stale read. The safe default, ReadIndex, confirms leadership with a heartbeat round trip per read batch.

A **leader lease** removes that round trip. Each successful heartbeat round also grants the leader a lease **shorter than the election timeout**. The invariant is that while the lease is unexpired *by the leader's own clock*, no rival can have completed an election, so a local read is linearizable at zero additional network cost. [TiKV](https://tikv.org/blog/lease-read/) and YugabyteDB both implement this, and YugabyteDB applies it to low-latency reads in geo-distributed clusters.

The exposure is that correctness now depends on the assumed drift bound holding. If the leader's clock runs slow, or followers' clocks run fast, beyond that bound, the lease expires later on the leader than in real time; a new leader is elected while the old one still considers its lease valid, and the old leader serves stale reads. This is a linearizability violation. The LeaseGuard work examines lease safety in Raft implementations and proposes a lease design intended to hold under clock anomalies and leader pauses.

## Sizing the term against drift

Three consequences follow from the drift-bound assumption.

- **Measure conservatively at both ends.** The holder starts its timer when it *sent* the request; the grantor starts when it *granted*. The holder's view of the term is strictly shorter, so the holder considers the lease dead before the grantor reassigns it.
- **Budget for the drift rate.** With maximum drift rate ρ, a holder should treat a term *T* as expiring at *T*·(1 − ρ) and a grantor should wait *T*·(1 + ρ) before reassigning.
- **Pauses defeat term arithmetic.** A garbage-collection or virtual-machine pause between "lease checked valid" and "write issued" invalidates the check regardless of how the term was sized. Guarded operations must stay short and re-check the deadline with margin; for writes to an external resource, the resource itself must enforce a fencing token, as described in the [locking article](/articles/sys-patterns/2026-08-11-distributed-locking-fencing-tokens). The lease states when the holder must stop; the token makes stopping enforceable.

### Implementation sketch (Scala)

The holder-side view of a lease: a deadline read from a monotonic clock, discounted by the drift bound, with a guard that refuses to run an action whose completion cannot be shown to fall inside the term.

```scala
// requestSentAtNanos, not the reply time: the request's flight time is the holder's loss.
final case class Lease(id: Long, requestSentAtNanos: Long, termNanos: Long):
  /** Holder-side expiry, shortened by the drift rate rho. */
  def expiresAtNanos(rho: Double): Long =
    requestSentAtNanos + (termNanos * (1.0 - rho)).toLong

final class LeaseHolder(rho: Double, guardMarginNanos: Long):
  @volatile private var current: Option[Lease] = None

  def onRenewed(lease: Lease): Unit = current = Some(lease)

  /** Runs f only if the whole call is expected to finish inside the term.
    * The deadline is re-read after f, so a pause during f is detected. */
  def guarded[A](budgetNanos: Long)(f: => A): Option[A] = current match
    case None => None
    case Some(lease) =>
      val deadline = lease.expiresAtNanos(rho) - guardMarginNanos
      if System.nanoTime() + budgetNanos > deadline then None
      else
        val a = f
        if System.nanoTime() > deadline then None // paused past expiry: discard
        else Some(a)
```

Discarding the result rather than publishing it is the only local remedy: by the time the pause is observed, another holder may already own the grant, and only the external resource can reject the late write.

## Pitfalls

- **Renewing at the expiry instead of a fraction of the term.** A single lost renewal round trip then loses the lease; renewing at roughly TTL/3 tolerates two consecutive losses within one term.
- **Starting the holder's timer on the grant reply rather than on the request.** The network delay of the request is then unaccounted for, and the holder believes the lease is live after the grantor has reassigned it.
- **A leader lease longer than the election timeout.** A rival can win an election while the incumbent's lease is still valid by its own clock, and the incumbent's local reads become stale.
- **Wall-clock timers for lease deadlines.** An administrator or Network Time Protocol (NTP) step adjustment moves the deadline, shortening or extending the term arbitrarily; a monotonic clock is not subject to the step.
- **Checking the lease and then performing an unbounded operation.** The check is valid only at the instant it is taken; any pause or slow call afterwards can carry the operation past expiry.
- **Treating lease expiry as proof the holder has stopped.** Expiry only revokes authority; a partitioned or paused holder may still issue writes, which is why the resource must reject them by fencing token.
