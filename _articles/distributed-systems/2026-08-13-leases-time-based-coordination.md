---
title: "Leases: time-bound ownership as a coordination primitive"
date: 2026-08-13
track: distributed-systems
summary: "A lease is ownership with an expiry date: hold it, renew it, and if you crash, the system heals itself when the clock runs out. From Gray & Cheriton's 1989 paper through Chubby's master leases and etcd TTLs to Raft leader leases that serve reads without a network round trip."
reading_time: 5
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

Every "who owns this right now?" problem — cache validity, leader election, work-queue assignment — has the same failure mode: the owner dies holding the grant. Permanent grants block forever; the fix is to make ownership **expire**. A *lease* is a grant of authority for a bounded term, renewable by the holder, and reclaimable by everyone else once the term lapses. It is the primitive under half the systems you'll be asked about in interviews. (This article is about leases as renewable time-based ownership; the enforcement side — fencing tokens, the Redlock debate — is covered in [distributed locking done right](/articles/sys-patterns/2026-08-11-distributed-locking-fencing-tokens).)

## The original idea: Gray & Cheriton, 1989

The lease paper is about file caches, but the mechanism is general. A server grants a client a lease over some data for term *T*. While the lease is valid, the client may serve cached reads locally, and the server promises to contact the client before accepting a conflicting write. The magic is what happens on failure: **nothing**. If the client crashes, the server just waits out the remaining term and proceeds. If the network partitions, same. No failure detector, no reconciliation protocol — time itself is the recovery mechanism.

The term is a tuning knob. Short leases (the paper analyzed terms around 10 s) mean fast recovery after a crash but constant renewal traffic; long leases amortize renewals but make writers wait longer when a lease holder vanishes. Crucially, the paper's correctness argument needs only **bounded clock drift rate** — both sides measure the same duration on their own clocks — never synchronized wall clocks.

## Chubby: leases all the way down

Google's Chubby lock service (Burrows, OSDI 2006) is leases stacked three deep. The elected master holds a **master lease**: the replicas promise not to elect anyone else while it lasts, and the master keeps extending it by winning quorum votes — so clients can trust one address without a consensus round per request. Clients, in turn, hold **session leases** extended by KeepAlive RPCs (12 s default); every lock and cached file handle a client holds is scoped to that session. When a master dies, clients enter a *grace period* (about 45 s) in which their session is "in jeopardy" but not dead, long enough for the new master to take over and honor old sessions. The whole design is the Gray–Cheriton trade dressed up for planet scale: renewable time-bound promises instead of perfect failure detection.

## etcd leases in practice

etcd exposes the primitive directly: a lease is an object with a TTL; keys are attached to it; if keepalives stop, the keys vanish atomically. This is how Kubernetes leader election and service registration work underneath.

```console
$ etcdctl lease grant 60
lease 694d77aa9e38260f granted with TTL(60s)
$ etcdctl put /leader/api node-42 --lease=694d77aa9e38260f
$ etcdctl lease keep-alive 694d77aa9e38260f    # blocks, renews periodically
$ etcdctl lease timetolive 694d77aa9e38260f --keys
```

The same flow in Go with `clientv3` — the client library renews at roughly TTL/3 intervals for you:

```go
cli, _ := clientv3.New(clientv3.Config{Endpoints: []string{"localhost:2379"}})
lease, _ := cli.Grant(ctx, 10) // 10 s TTL
cli.Put(ctx, "/leader/api", "node-42", clientv3.WithLease(lease.ID))
ch, _ := cli.KeepAlive(ctx, lease.ID) // background renewal channel
// if this process stalls or dies, /leader/api disappears within 10 s
```

Kill the keepalive and the key evaporates when the TTL runs out — cleanup with no janitor process.

## Leader leases: reads without a round trip

The subtlest use of leases is a *read optimization* in Raft-family systems. A Raft leader can't just answer reads from local state — it might have been deposed and not know it, which is exactly a stale read. The safe default (ReadIndex) confirms leadership with a heartbeat round trip per read batch. A **leader lease** removes that round trip: each successful heartbeat also grants the leader a lease slightly shorter than the election timeout. While the lease is unexpired *by the leader's own clock*, no rival can have won an election, so local reads are linearizable — zero extra network cost. [TiKV](https://tikv.org/blog/lease-read/) and YugabyteDB both ship this, and YugabyteDB leans on it for low-latency reads in geo-distributed clusters.

The catch: correctness now depends on the clock-drift bound actually holding. If the leader's clock runs slow (or the followers' run fast) beyond the assumed bound, the lease "expires" later on the leader than in reality, a new leader gets elected, and the old one serves stale reads — a real linearizability violation, not a theoretical one. The LeaseGuard work (2026) audits how production Raft systems implement leases, shows several are unsafe under clock anomalies or leader pauses, and proposes a design that stays safe even then. If an interviewer asks "what breaks leases?", this is the answer they want.

## Clock skew: sizing the term honestly

Practical rules that fall out of the drift-bound assumption:

- **Measure conservatively on both ends.** The holder starts its lease timer when it *sent* the request; the grantor starts when it *granted*. The holder's view is strictly shorter, so both agree the lease is dead before anyone else acts.
- **Budget for drift.** With max drift rate ρ (a few ms/s is a sane engineering bound), a holder should treat a lease of term *T* as expiring at *T*·(1 − ρ), and a grantor should wait *T*·(1 + ρ) before reassigning.
- **Pauses beat clocks.** A GC or VM pause between "check lease valid" and "do the write" defeats any term math. Keep guarded operations short, re-check the deadline with margin — and for writes to external resources, have the resource enforce a fencing token, as covered in the [locking article](/articles/sys-patterns/2026-08-11-distributed-locking-fencing-tokens). Lease says when *you* must stop; the token makes stopping enforceable.

**Try next:** grant a 5-second etcd lease with a key attached, run `etcdctl lease keep-alive` in one terminal, then hit Ctrl-Z to SIGSTOP it — watch the key vanish, then `fg` and see the renewal fail: that's a GC pause killing a lease in miniature.
