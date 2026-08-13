---
title: "Coordination recipes: building locks, barriers, and elections from ZooKeeper and etcd primitives"
date: 2026-08-13
track: sys-patterns
summary: "ZooKeeper ships four primitives — znodes, ephemerals, sequence numbers, watches — and etcd ships revisions, leases, watch, and transactions. Every classic coordination recipe (herd-free locks, barriers, group membership, elections) is a short composition of them, and interviewers love asking you to derive one."
reading_time: 5
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

ZooKeeper and etcd don't ship a "lock service." They ship a small set of primitives, and the [official recipes](https://zookeeper.apache.org/doc/current/recipes.html) are compositions you're expected to build client-side. Deriving a recipe from primitives is a staple systems-interview exercise — and the derivations are short once you know the four moving parts on each side.

## The primitives

**ZooKeeper:** a tree of *znodes*, plus three modifiers. An **ephemeral** znode is deleted automatically when its creator's session dies — that's your failure detector. A **sequential** znode gets a server-assigned, monotonically increasing suffix (`lock-0000000042`) — that's your global ordering. A **watch** is a one-shot notification that a znode changed — that's your "don't poll" mechanism. Reads see a consistent order because every update goes through the ZAB-replicated leader.

**etcd:** a flat keyspace where every write bumps a cluster-wide, monotonic **revision**; each key remembers the revision that created it (`CreateRevision`). A **lease** is a TTL object you attach to keys and keep alive with heartbeats — keys vanish when the lease expires (etcd's ephemeral). **Watch** streams changes from any revision, so you can't miss events between "read" and "watch." **Txn** is a mini-transaction: compare (value, version, revision) then atomically apply puts/deletes — compare-and-swap as a server primitive.

The mapping to remember: *ephemeral ↔ lease*, *sequence number ↔ CreateRevision*, *watch ↔ watch-from-revision*.

## The lock recipe, without the herd

Naive lock: everyone tries to create `/lock`, losers watch it, deletion wakes them all, they stampede. With a thousand contenders, every release triggers a thousand wakeups and a thousand new create attempts — the **herd effect**. The recipe that avoids it:

1. Create an ephemeral **sequential** znode `/_locknode_/lock-`, yielding e.g. `lock-0000000017`.
2. `getChildren("/_locknode_")` *without* a watch.
3. If your sequence number is the lowest, you hold the lock.
4. Otherwise, set a watch (via `exists()`) on the child with the **next-lowest** sequence number only.
5. When that watch fires, go to step 2.

Each znode is watched by exactly one client, so a release (or a crash — the ephemeral vanishes) wakes exactly one waiter: O(1) wakeups instead of O(n), and FIFO fairness for free. etcd's `concurrency.Mutex` is the same shape with different nouns: put `prefix/<leaseID>` under a lease, and you hold the lock iff your key has the lowest `CreateRevision` in the prefix; otherwise watch for deletion of your immediate predecessor by revision.

```go
cli, _ := clientv3.New(clientv3.Config{Endpoints: []string{"localhost:2379"}})
sess, _ := concurrency.NewSession(cli, concurrency.WithTTL(15)) // lease-backed
mu := concurrency.NewMutex(sess, "/locks/reindex")
if err := mu.Lock(context.TODO()); err != nil { log.Fatal(err) }
doTheWork()                    // guard writes with mu.Header().Revision as a fencing token
mu.Unlock(context.TODO())
```

Or from the shell — `etcdctl lock` holds the lock for the lifetime of a subcommand, and `elect` campaigns on a prefix:

```console
$ etcdctl lock /locks/reindex ./run-reindex.sh
$ etcdctl elect scheduler node-a     # blocks until elected, prints the leader key
```

One deliberate omission here: *holding* the lock does not make your writes safe — a paused process can outlive its lease and write anyway. That failure mode, Redlock, and fencing tokens are covered in [distributed locking done right](/articles/sys-patterns/2026-08-11-distributed-locking-fencing-tokens); the recipe's contribution is fair, herd-free queuing, and it hands you the fencing token for free (the sequence number / `CreateRevision`).

## Leader election is the same recipe

Election is the lock recipe where the holder never intends to release: lowest sequence number (ZK) or lowest `CreateRevision` (etcd `Election.Campaign`) *is* the leader; everyone else watches their predecessor, so leader death promotes exactly one successor with no thundering herd. etcd's [Election API](https://etcd.io/docs/v3.6/dev-guide/api_concurrency_reference_v3/) adds `Proclaim` (publish leader metadata), `Observe` (stream leadership changes to non-candidates), and `Resign`. What you *do* with leadership — Kubernetes-style lease-based election, split-brain handling, the operational pattern — is the subject of [the leader election pattern](/articles/sys-patterns/2026-07-25-leader-election-pattern), and leases as a primitive get their own article in this track today; this recipe is just the queue underneath.

## Barriers

A **barrier** parks a group of processes until a condition holds. Simple version: everyone watches barrier znode `/b`; when the coordinator deletes it, all proceed (here the herd wakeup is the *point*). The **double barrier** synchronizes entry and exit of N workers: on entry, each creates an ephemeral child under `/b` and waits until there are N children; on exit, each deletes its child and waits until the children are gone. The recipe doc's refinement is worth quoting in an interview: instead of every waiter watching the child list (herd again), the first-entered process watches only a `ready` marker, and lowest/highest-sequence processes do the watching on exit — same watch-one-node trick as the lock.

## Group membership

Membership falls out of ephemerals: each member creates `/group/member-<seq>` (ephemeral) carrying its address; `getChildren("/group", watch=true)` gives any observer the live roster plus a notification on join/leave. Crash = session expiry = automatic removal — no deregistration path to forget. In etcd: each member `Put`s `members/<id>` bound to its lease and keeps the lease alive; observers `Get` the prefix and `Watch` from that revision. This is the primitive under every "how does the cluster know who's in it" answer, and it's exactly how etcd-backed service discovery works.

## What to say when asked "ZooKeeper or etcd?"

Same consistency story (leader-replicated log: ZAB vs Raft), same recipe shapes. Differences that matter: etcd's revisions are global and exposed (natural fencing tokens and resumable watches); ZK's watches are one-shot and can drop events between fire and re-register unless you re-read; etcd speaks gRPC/HTTP with leases you manage explicitly, ZK has sessions managed by thick client libraries (in practice, use Curator, which ships these recipes pre-built). Either way, the winning interview move is the derivation: *ephemeral for liveness, sequence/revision for order, watch-the-predecessor for herd-free wakeup*.

**Try next:** run a local etcd, open three terminals with `etcdctl lock /locks/demo sleep 60`, and in a fourth run `etcdctl get --prefix /locks/demo` to watch the queue of lease-keyed waiters ordered by create revision.
