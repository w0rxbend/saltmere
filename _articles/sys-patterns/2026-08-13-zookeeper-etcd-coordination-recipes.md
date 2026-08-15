---
title: "Coordination recipes: building locks, barriers, and elections from ZooKeeper and etcd primitives"
date: 2026-08-13
summary: "ZooKeeper exposes four primitives — znodes, ephemerals, sequence numbers, watches — and etcd exposes revisions, leases, watch, and transactions. Every classic coordination recipe (herd-free locks, barriers, group membership, elections) is a short composition of them."
track: sys-patterns
reading_time: 6
tags: [zookeeper, etcd, coordination, distributed-lock, leader-election]
sources:
  - title: "Apache ZooKeeper — Recipes and Solutions"
    url: "https://zookeeper.apache.org/doc/current/recipes.html"
  - title: "etcd v3 — Concurrency API reference (Lock, Election)"
    url: "https://etcd.io/docs/v3.6/dev-guide/api_concurrency_reference_v3/"
  - title: "etcd clientv3/concurrency package (Go)"
    url: "https://pkg.go.dev/go.etcd.io/etcd/client/v3/concurrency"
  - title: "etcdctl README — lock and elect commands"
    url: "https://github.com/etcd-io/etcd/blob/main/etcdctl/README.md"
---

**Gist.** Neither ZooKeeper nor etcd ships a lock service; both ship a handful of primitives from which locks, barriers, elections and membership are composed client-side, as documented in the [official recipes](https://zookeeper.apache.org/doc/current/recipes.html). The composition that makes the recipes scale is **each waiter watching exactly one predecessor**, which converts a release from O(n) wakeups into O(1). The cost is that correctness now lives in client code: the ordering, the watch re-registration and the liveness assumption behind ephemerals or leases are the caller's responsibility, and a lock held past a lease expiry protects nothing.

## The primitives

**ZooKeeper:** a tree of *znodes*, plus three modifiers. An **ephemeral** znode is deleted automatically when its creator's session dies, which supplies the failure detector. A **sequential** znode receives a server-assigned, monotonically increasing suffix (`lock-0000000042`), which supplies a total order. A **watch** is a one-shot notification that a znode changed, which removes the need to poll. Every update passes through the ZAB-replicated (ZooKeeper Atomic Broadcast) leader, so writes are totally ordered; reads are served by whichever server the client is connected to and may lag that order until a `sync()`.

**etcd:** a flat keyspace in which every write increments a cluster-wide monotonic **revision**; each key records the revision that created it (`CreateRevision`). A **lease** is a time-to-live (TTL) object attached to keys and renewed by heartbeats; keys bound to it disappear when it expires, which is etcd's analogue of an ephemeral. **Watch** streams changes *from a caller-supplied revision*, so no event is lost in the gap between a read and the establishment of the watch, provided that revision has not been compacted away. **Txn** is a mini-transaction: compare on value, version or revision, then atomically apply puts and deletes — compare-and-swap as a server-side primitive.

The correspondence is: *ephemeral ↔ lease*, *sequence number ↔ CreateRevision*, *watch ↔ watch-from-revision*.

## The lock recipe, without the herd

The naive lock has every contender attempt to create `/lock`, with losers watching that single node. Its deletion wakes all of them and they retry together — the **herd effect**: with n contenders, each release triggers n notifications and n create attempts. The documented recipe avoids this:

1. Create an ephemeral **sequential** znode `/_locknode_/lock-`, yielding for example `lock-0000000017`.
2. Call `getChildren("/_locknode_")` *without* a watch.
3. If the caller's sequence number is the lowest, the lock is held.
4. Otherwise set a watch (via `exists()`) on the child with the **next-lowest** sequence number only.
5. When that watch fires, return to step 2.

**Each znode is watched by exactly one client**, so a release — or a crash, since the ephemeral then vanishes — wakes exactly one waiter: O(1) notifications per handover rather than O(n), with first-in-first-out (FIFO) fairness as a by-product. Step 2 is re-executed rather than assuming promotion, because the watched predecessor may have vanished through session expiry rather than release, leaving a different node lowest.

etcd's `concurrency.Mutex` has the same shape with different nouns: put `prefix/<leaseID>` under a lease; the lock is held **iff that key has the lowest `CreateRevision` in the prefix**; otherwise watch for deletion of the immediate predecessor by revision.

```go
cli, _ := clientv3.New(clientv3.Config{Endpoints: []string{"localhost:2379"}})
sess, _ := concurrency.NewSession(cli, concurrency.WithTTL(15)) // lease-backed
mu := concurrency.NewMutex(sess, "/locks/reindex")
if err := mu.Lock(context.TODO()); err != nil { log.Fatal(err) }
doTheWork()                    // guard writes with mu.Header().Revision as a fencing token
mu.Unlock(context.TODO())
```

The same recipes are reachable from the shell: `etcdctl lock` holds the lock for the lifetime of a subcommand, and `elect` campaigns on a prefix.

```console
$ etcdctl lock /locks/reindex ./run-reindex.sh
$ etcdctl elect scheduler node-a     # blocks until elected, prints the leader key
```

One omission is deliberate. *Holding* the lock does not make the holder's writes safe: a paused process can outlive its lease and write afterwards. That failure mode, Redlock, and fencing tokens are treated in [distributed locking done right](/articles/sys-patterns/2026-08-11-distributed-locking-fencing-tokens). The contribution of this recipe is fair, herd-free queuing, and it yields the fencing token as a side effect — the sequence number or the `CreateRevision`.

### Implementation sketch (Scala)

The load-bearing part is not the client library but the predecessor selection: given the current child list and the caller's own node, decide between *acquired* and *watch exactly one other node*.

```scala
enum Step:
  case Acquired
  case Watch(node: String)

/** Children as returned by getChildren; `self` is this client's own child name.
  * Ordering is by the server-assigned sequence suffix — a fixed ten-digit,
  * zero-padded number — not by the whole child name. */
def nextStep(children: Seq[String], self: String): Step =
  def seq(n: String): Long = n.takeRight(10).toLong
  val ordered = children.sortBy(seq)
  val i = ordered.indexOf(self)
  if i <= 0 then Step.Acquired else Step.Watch(ordered(i - 1))

def acquire(self: String)(
    getChildren: () => Seq[String],
    watchDeletion: String => Boolean   // false if the node is already gone
): Unit =
  var done = false
  while !done do
    nextStep(getChildren(), self) match
      case Step.Acquired    => done = true
      case Step.Watch(prev) =>
        watchDeletion(prev)            // returning on a vanished node is correct:
                                       // the loop re-reads instead of assuming promotion
```

`i <= 0` covers both the lowest-sequence case and the case where the caller's own node has disappeared from the list, which is a session-expiry signal rather than an acquisition; a production client checks that separately.

## Leader election is the same recipe

Election is the lock recipe in which the holder does not intend to release. The lowest sequence number (ZooKeeper) or lowest `CreateRevision` (etcd `Election.Campaign`) identifies the leader; every other candidate watches its predecessor, so the death of a leader promotes exactly one successor without a herd. etcd's [Election API](https://etcd.io/docs/v3.6/dev-guide/api_concurrency_reference_v3/) adds `Proclaim` (publish leader metadata), `Observe` (stream leadership changes to non-candidates) and `Resign`. What is done *with* leadership — lease-based election in Kubernetes, split-brain handling, the operational pattern — is the subject of [the leader election pattern](/articles/sys-patterns/2026-07-25-leader-election-pattern); this recipe is the queue underneath it.

## Barriers

A **barrier** parks a group of processes until a condition holds. In the simple form every process watches a barrier znode `/b` and proceeds when the coordinator deletes it; here the herd wakeup is the intended behaviour. The **double barrier** synchronises both entry and exit of N workers: on entry each process creates an ephemeral child under `/b` and waits until N children exist; on exit each deletes its child and waits until no children remain. The documented protocol keeps each waiter on one watch rather than on the whole child list. On entry a process sets a watch on a `ready` marker before creating its own child, then counts the children; the process that finds N of them creates `ready`, releasing the rest. On exit the process holding the lowest-numbered child waits on the highest-numbered one and every other process waits on the lowest, so each wait is again a watch on a single named node.

## Group membership

Membership follows from ephemerals. Each member creates `/group/member-<seq>` as an ephemeral node carrying its address; `getChildren("/group", watch=true)` gives an observer the live roster plus a notification on join or leave. A crash becomes a session expiry, which becomes automatic removal, so there is **no deregistration path that can be forgotten**. In etcd each member puts `members/<id>` bound to its lease and keeps that lease alive; observers `Get` the prefix and `Watch` from the returned revision. This is the mechanism under etcd-backed service discovery.

## Choosing between them

The consistency story is comparable — a leader-replicated log in both cases, ZAB versus Raft — and the recipe shapes are the same. The differences that bear on client code: etcd's revisions are global and exposed, giving natural fencing tokens and resumable watches; **ZooKeeper watches are one-shot** and events can be missed between firing and re-registration unless the client re-reads state, which is why step 2 of the lock recipe re-reads rather than trusting the notification; etcd is accessed over gRPC with leases the client manages explicitly, while ZooKeeper sessions are managed by thick client libraries, with Curator shipping these recipes pre-built.

## Pitfalls

- **Watching the whole child list instead of the predecessor.** Symptom: release latency grows with contender count and the server's outbound notification traffic spikes on every handover. Cause: n watches on one node produce n wakeups per release.
- **Treating a fired watch as acquisition.** Symptom: two clients believe they hold the lock. Cause: the predecessor may have vanished through session expiry while an even lower-sequence node still exists; the child list must be re-read.
- **Sorting child names by the whole string rather than by the sequence suffix.** Symptom: the queue order stops matching arrival order once a parent holds children created under more than one prefix. Cause: the padded suffix makes string order agree with numeric order only among names sharing an identical prefix; the recipe defines the order on the suffix alone.
- **Assuming the lock makes subsequent writes safe.** Symptom: a paused or garbage-collection-stalled holder writes after its ephemeral or lease has gone. Cause: expiry is decided by the server on a clock the holder does not observe; the guarded resource must reject stale fencing tokens.
- **Retaining a non-ephemeral node for a lock or membership entry.** Symptom: a lock is never released and a dead member stays in the roster indefinitely. Cause: only ephemerals and lease-bound keys are removed on session or lease loss.
- **Losing events between a read and a watch in etcd.** Symptom: a waiter blocks forever on a deletion that already happened. Cause: the watch was started at "now" rather than from the revision returned by the read.
