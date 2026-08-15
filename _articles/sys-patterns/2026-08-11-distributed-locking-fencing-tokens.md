---
title: "Distributed locking done right: a lock is not enough, you need a fencing token"
date: 2026-08-11
track: sys-patterns
summary: "A distributed lock stops two workers colliding on the happy path. But a GC pause outlives a lease and your zombie process writes anyway. The fix isn't a better lock, it's a monotonic fencing token the resource itself checks."
reading_time: 6
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

You have three workers and one job that must run once at a time: drain a queue, reconcile state, write a file to shared storage. The textbook answer is a distributed lock. Grab it, do the work, release it. The nasty part isn't grabbing the lock — it's that a lock, on its own, does not actually guarantee mutual exclusion at the moment you write. This is the single most misunderstood thing in the space, and it shows up in interviews constantly.

## The classic lock, three ways

The Redis one-liner is `SET resource_key random_value NX PX 30000`: set the key only if it doesn't exist (`NX`), with a 30-second expiry (`PX`) so a dead holder doesn't wedge the lock forever. The `random_value` is the fencing you use *at release* — a Lua script deletes the key only if the value still matches yours, so you never delete a lock someone else has since acquired. For a single Redis node that's the whole recipe. To survive one node failing, [Redlock](https://redis.io/docs/latest/develop/use/patterns/distributed-locks/) runs the same acquire against N independent masters and declares success only if a majority ack within the validity window.

The consensus-store variants look different but rhyme. In [ZooKeeper](https://zookeeper.apache.org/doc/r3.8.5/recipes.html) each contender creates an **ephemeral sequential** znode under a lock path; the lowest sequence number holds the lock, and "ephemeral" means the node vanishes if the client's session dies — automatic release on crash. [etcd](https://pkg.go.dev/go.etcd.io/etcd/client/v3/concurrency) does the same with a lease-backed `Session` and a `Mutex` built on it. All of these are leases: locks with a deadline you must renew.

## Where the lease betrays you

Here is the hazard Martin Kleppmann [raised in 2016](https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html), and it has nothing to do with Redis bugs. Client 1 acquires the lock. Then it stalls — a stop-the-world GC pause, a hypervisor freeze, a paging storm, a slow network. While it's frozen, the lease expires. The lock service, correctly, hands the lock to Client 2, which does its work. Then Client 1 wakes up. It has no idea any time passed. It still *believes* it holds the lock, and it issues its write.

Two clients wrote to the resource. Silent corruption.

> "If the GC pause lasts longer than the lease expiry period, and the client doesn't realise that it has expired, it may go ahead and make some unsafe change."

Checking the lease "am I still the holder?" right before writing does not save you, because the pause can land *between* the check and the write. No amount of tightening the lock protocol closes this, because the failure is on the client side of the wire. This is the same zombie-leader problem the [leader-election pattern](/articles/sys-patterns/2026-07-25-leader-election-pattern) warns about, and the reason electing a single leader — whether via a lease or a [bully/ring election](/articles/distributed-systems/2026-07-30-election-algorithms-bully-ring) — still isn't sufficient by itself.

## The Kleppmann vs antirez debate, fairly

Kleppmann's second, sharper critique is that Redlock's safety leans on **timing assumptions**: bounded clock drift, bounded process pauses, bounded network delay. In an asynchronous system none of those are guaranteed, so a lock built for *correctness* shouldn't depend on them.

Salvatore Sanfilippo (antirez) [pushed back](https://antirez.com/news/101), and his points are worth taking seriously. He argues Redlock only needs each process to "count 5 seconds with a maximum of 10% error" — a much weaker assumption than perfectly synced wall clocks — and that its checks of elapsed time before and after acquisition make it robust to unbounded message delays. He also makes a pointed observation about fencing tokens themselves: **lock-acquisition order does not necessarily match the order clients actually touch the resource**, so a token only helps if the resource is the thing enforcing it. Both are right about different questions. Kleppmann is right that Redlock is not a correctness lock under adversarial timing; antirez is right that for many real systems the assumptions hold and the ceremony isn't free. The resolution isn't picking a winner — it's fencing.

## The actual fix: a fencing token

A **fencing token** is a number the lock service hands out that increases every time the lock is granted. The client stamps every write with its token. The resource being protected remembers the highest token it has ever accepted and **rejects anything lower**. The zombie's write carries a stale, smaller token and bounces off the storage layer, no matter how confused the client is.

```python
# Lock service: acquire() returns a monotonically increasing token.
token = lock.acquire("job-42")   # e.g. 33, then next holder gets 34...

# The PROTECTED RESOURCE enforces safety — not the client, not the lock.
def write(payload, token):
    # last_seen persisted atomically alongside the data (CAS / row lock / txn)
    if token <= storage.last_seen_token:
        raise StaleToken(f"rejecting {token}, already saw {storage.last_seen_token}")
    storage.last_seen_token = token
    storage.data = payload
```

The load-bearing line is `if token <= last_seen: reject`. Safety lives *in the resource*, which is the only party present at the moment of the write. You do not need to invent the counter: etcd gives you a global, monotonically increasing **revision** on every mutation, and ZooKeeper gives you the **zxid** (and the znode's `cversion`/sequence) — both are ready-made tokens. Redis has no native monotonic counter across the Redlock nodes, which is exactly the gap Kleppmann highlighted; you'd bolt an `INCR` on top and inherit its own single-point concerns.

## Efficiency locks vs correctness locks

The decision that actually matters is *what happens if the lock fails*.

| | Efficiency lock | Correctness lock |
|---|---|---|
| Purpose | Avoid redundant work | Prevent data corruption |
| If two hold it | Wasteful, harmless (double-send an email) | Catastrophic (double-charge, corrupt file) |
| Redis/Redlock alone | Fine | **Not fine** |
| Needs fencing? | No | **Yes** |

If occasional double-work is merely wasteful — you sent one duplicate email, recomputed one cache entry — a plain lease is fine and fencing is over-engineering. If a second writer means a corrupted ledger, you need a fencing token enforced at the resource, full stop. Reach for a lock only after asking which of these you're building; most bugs come from using an efficiency lock where correctness was required.

**Try next:** stand up etcd, take a lock via `clientv3/concurrency`, and record the `Revision` returned as your token. Then SIGSTOP the holder past the lease TTL, let a second client grab the lock at a higher revision and write, resume the first, and confirm its lower-revision write is rejected by your `if token <= last_seen` guard.
