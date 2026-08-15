---
title: "Chain replication and CRAQ: strong consistency with scalable reads"
date: 2026-07-30
track: distributed-systems
summary: "Chain replication makes writes linearizable with a simple failure model, but confines reads to the tail node. CRAQ removes that bottleneck by letting every node answer reads under a clean/dirty version check. Both protocols are described, with an implementation sketch."
reading_time: 6
tags: [chain-replication, craq, replication, linearizability, consistency, quorum]
sources:
  - title: "Chain Replication for Supporting High Throughput and Availability (OSDI 2004) — van Renesse & Schneider"
    url: "https://www.usenix.org/legacy/event/osdi04/tech/full_papers/renesse/renesse.pdf"
  - title: "Object Storage on CRAQ: High-throughput chain replication for read-mostly workloads (USENIX ATC 2009) — Terrace & Freedman"
    url: "https://www.cs.princeton.edu/courses/archive/fall19/cos418/papers/craq.pdf"
  - title: "MIT 6.824 Lecture 9 — CRAQ (notes)"
    url: "https://timilearning.com/posts/mit-6.824/lecture-9-craq/"
  - title: "go-craq — a Go implementation of CRAQ"
    url: "https://github.com/despreston/go-craq"
---

**Gist.** Replicating an object across *R* nodes while keeping reads linearizable normally costs a quorum round per operation. Chain replication (van Renesse & Schneider, OSDI 2004) obtains linearizability by ordering the replicas in a line and serving all reads from one end, so no operation requires consensus; the cost is that **a single node — the tail — absorbs the entire read load** while the other *R−1* replicas hold the same committed data they may not serve. CRAQ (Terrace & Freedman, USENIX ATC 2009) keeps that write path and allows any node to answer reads, at the cost of **an extra round trip to the tail for every object with a write in flight**.

## The chain and its invariant

The *R* replicas of an object are arranged in a total order: a **head**, zero or more middle nodes, and a **tail**. Operations enter at fixed ends.

- A **write** enters at the **head**. The head applies it locally and forwards it to its successor. Each node applies and forwards in turn. When the write reaches the **tail**, the tail applies it and sends an **acknowledgement** back up the chain.
- A **read**, in plain chain replication, is served **only by the tail**.

The propagation order yields the invariant that carries the protocol: **the tail has applied a write if and only if every node has applied it**, because a write reaches the tail only after passing through all predecessors. The tail's state is therefore exactly the set of committed writes, and a read served there returns the latest committed value without contacting any other node. This is the structural difference from quorum systems, where a read must contact several replicas and reconcile their answers.

Because the order is fixed rather than negotiated per operation, the recovery cases are enumerable:

- **Head fails.** Its successor becomes the new head. Writes the old head had accepted but not yet forwarded are lost; they were never acknowledged, so no client was told they committed.
- **Tail fails.** Its predecessor becomes the new tail. The predecessor holds everything the old tail held, plus possibly writes not yet acknowledged; promoting it **converts those extra writes into committed writes**, which is safe because it can only add to the committed prefix, never remove from it.
- **Middle node fails.** Its predecessor is reconnected to its successor, and a reconciliation step replays the writes the successor had not yet received.

A separate fault-tolerant **master** — in practice backed by Paxos or Raft — monitors liveness and publishes the current chain membership. **Consensus is confined to the metadata path; the data path carries no consensus round.**

## The tail as a bottleneck

Every read in plain chain replication lands on one node, so **read throughput is bounded by the capacity of a single replica regardless of R**. Adding replicas increases durability and write-path length but not read capacity. For read-mostly workloads — object stores, caches, configuration services — most of the replicated hardware serves no read traffic.

## CRAQ: apportioned queries

CRAQ (Chain Replication with Apportioned Queries) leaves the write path unchanged and makes reads servable from **any** node without weakening the guarantee. The mechanism is per-object versioning with a two-state tag.

Each node stores, per object, possibly **multiple versions**, each marked **clean** or **dirty**:

- On receiving a write via propagation, a node appends the new version and marks it **dirty**: seen, but not known to be committed.
- When the acknowledgement travels back up the chain, each node marks that version **clean** and discards older versions.

A read at an arbitrary node then follows two cases:

1. **The newest local version is clean.** Return it immediately; no coordination.
2. **The newest local version is dirty.** The node does not guess. It queries the **tail** for the latest committed version number of that object and returns **that version from its own local store**.

The version query transfers a version number rather than the object, and it arises **only for objects with a write in flight**. Under a read-mostly workload most reads take case 1 and are served entirely locally, converting the *R−1* non-tail replicas into read capacity. The guarantee is preserved because the tail remains the sole authority on what is committed: a node never returns a version the tail has not committed. The other half of the argument is the propagation order. A version can only commit at the tail after passing through every node, so **a node whose newest local version is clean cannot be missing a newer committed version** — if such a version existed, that node would already hold it, dirty or clean, and case 2 would apply instead.

CRAQ additionally defines weaker read modes — eventual consistency, which returns the newest local version, possibly dirty, with no tail query, and a bounded variant that limits how stale that version may be. The strongly consistent mode described above is the one the protocol is presented under.

### Implementation sketch (Scala)

The read path is the load-bearing part; the write path is ordinary forwarding.

```scala
final case class Version(n: Long, value: Array[Byte], clean: Boolean)

trait Tail:
  def latestCommittedVersion(key: String): Long

final class CraqNode(isTail: Boolean, tail: Tail):
  // newest version last; at most one dirty suffix per key
  private val store = scala.collection.mutable.Map.empty[String, Vector[Version]]

  def read(key: String): Option[Array[Byte]] =
    store.get(key).flatMap { vs =>
      val newest = vs.last
      if newest.clean then Some(newest.value)
      else
        // dirty: the tail alone knows what is committed
        val committed = tail.latestCommittedVersion(key)
        vs.findLast(_.n == committed).map(_.value)
    }

  def onPropagate(key: String, n: Long, value: Array[Byte]): Unit =
    store.updateWith(key)(vs =>
      Some(vs.getOrElse(Vector.empty) :+ Version(n, value, clean = false))
    )
    if isTail then onAck(key, n) // tail commits, then acknowledges upstream

  def onAck(key: String, n: Long): Unit =
    store.updateWith(key)(_.map { vs =>
      // the ack retires every version below n
      vs.filter(_.n >= n).map(v => if v.n == n then v.copy(clean = true) else v)
    })
```

Complete implementations, such as `go-craq`, add the master, chain reconfiguration and per-key chains so that hot and cold objects do not share a bottleneck; the asymmetry above is the core.

## Applicability

The combination suits **read-mostly workloads requiring linearizability**, where the alternative is quorum-read latency: metadata services, session stores, configuration and feature-flag backends, small object stores. It suits write-heavy and geographically distributed write workloads poorly: **write latency is the sum of the per-hop latencies along the chain**, so it grows with chain length, and a distant tail adds that distance to every dirty read as well.

## Pitfalls

- **Sizing read capacity by replica count under plain chain replication.** Throughput plateaus once the tail saturates; the added replicas contribute durability only.
- **Assuming a CRAQ read is always local.** An object under sustained write traffic keeps its newest version dirty, so every read of it pays a round trip to the tail — a hot key can drive the tail load back toward the plain-chain case.
- **Treating writes lost at head failover as a bug.** Writes the old head had not forwarded were never acknowledged; a client that received no acknowledgement has no committed write to lose.
- **Placing the tail far from readers.** The tail's distance is charged to every dirty read and to the final hop of every write.
- **Sharing one chain across all keys.** A single hot object then saturates the chain for every other object; per-key or per-shard chains isolate them.
- **Reading from a node whose membership view is stale.** The master publishes chain membership; a node acting on an outdated view can answer as tail or query a node that is no longer the tail.
