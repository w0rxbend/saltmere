---
title: "Merkle Trees: Reconciling Replicas Without Shipping the Data"
date: 2026-07-27
track: distributed-systems
summary: "How Dynamo-style key-value stores use hash trees to locate the few keys that diverged between replicas, instead of comparing the whole dataset key by key."
reading_time: 6
tags: [merkle-tree, anti-entropy, read-repair, dynamo, cassandra, replication]
sources:
  - title: "Dynamo: Amazon's Highly Available Key-value Store (SOSP 2007)"
    url: "https://www.allthingsdistributed.com/files/amazon-dynamo-sosp2007.pdf"
  - title: "Manual repair: Anti-entropy repair (Apache Cassandra 3.x, DataStax docs)"
    url: "https://docs.datastax.com/en/cassandra-oss/3.x/cassandra/operations/opsRepairNodesManualRepair.html"
  - title: "Active Anti-Entropy (Riak KV docs)"
    url: "https://docs.riak.com/riak/kv/latest/learn/concepts/active-anti-entropy/index.html"
  - title: "Distributed Systems, 3rd ed. — van Steen & Tanenbaum"
    url: "https://www.distributed-systems.net/index.php/books/ds3/"
---

**Gist.** Quorum reads with `R + W > N` repair divergence only on keys that are read, so replicas of cold keys drift silently after a missed write, an expired hint or on-disk corruption. **Anti-entropy** — periodic pairwise reconciliation of a key range — closes that gap, and a **Merkle tree** (a hash tree over the range) reduces each comparison from a full scan to an exchange of hashes along the paths to the diverged leaves. The cost is that the tree is bound to a fixed key range: when ranges move, the trees covering them must be recalculated.

## The structure and the invariant

A Merkle tree is a hash tree. In Dynamo's words, "leaves are hashes of the values of individual keys. Parent nodes higher in the tree are hashes of their respective children" (§4.7). The invariant that makes the protocol work is **a node's hash is a function of the contents of its entire subtree**: equal hashes at a node imply — up to hash collision — equal contents beneath it.

The comparison protocol follows from that invariant:

- If the two **roots** match, every leaf underneath matches; the replicas are in sync and the exchange ends after one hash.
- If the roots differ, at least one leaf differs. The comparison recurses into children whose hashes disagree and **prunes** subtrees whose hashes agree.

Bandwidth is spent only along the paths to keys that diverged. One divergent key in a million costs a walk of depth `log2(leaves)` rather than a million comparisons.

Two preconditions are load-bearing. First, **both replicas must build the same-shaped tree over the same range**, otherwise the hashes compare structure rather than contents and every root differs. Second, **leaf construction must be order-independent**: replicas receive writes in different orders, so a leaf hash folded in arrival order diverges even when the contents agree. Sorting keys within a leaf bucket before folding restores that property.

Dynamo keeps "a separate Merkle tree for each key range (the set of keys covered by a virtual node)," so two nodes sharing a range compare only that range's tree. The scheme assumes consistent hashing to define the ranges, and vector clocks to establish which of two divergent versions supersedes the other — or that they are concurrent, in which case Dynamo defers the choice to the application.

### Implementation sketch (Scala)

Build over a range with a fixed number of leaf buckets, then diff. `tree(0)` is the root; children of node `i` are `2*i + 1` and `2*i + 2`.

```scala
import java.security.MessageDigest

type Hash = IndexedSeq[Byte]

def sha256(bs: Array[Byte]): Hash =
  MessageDigest.getInstance("SHA-256").digest(bs).toIndexedSeq

def bucket(key: String, numLeaves: Int): Int =
  // mask the sign bit rather than call abs: abs(Int.MinValue) is still negative
  (java.nio.ByteBuffer.wrap(sha256(key.getBytes).take(4).toArray).getInt & 0x7fffffff) % numLeaves

/** numLeaves must be a power of two and identical on both replicas. */
def build(kv: Map[String, String], numLeaves: Int = 1024): IndexedSeq[Hash] =
  val leaves = Array.fill[Hash](numLeaves)(IndexedSeq.empty)
  for k <- kv.keys.toSeq.sorted do            // sorted => arrival order irrelevant
    val i = bucket(k, numLeaves)
    leaves(i) = sha256((leaves(i) ++ s"$k=${kv(k)}".getBytes.toIndexedSeq).toArray)

  var level: IndexedSeq[Hash] = leaves.toIndexedSeq
  var tree = level
  while level.size > 1 do
    level = level.grouped(2).map(p => sha256((p(0) ++ p(1)).toArray)).toIndexedSeq
    tree = level ++ tree
  tree

/** Leaf indices whose contents differ; agreeing subtrees are never descended. */
def diff(a: IndexedSeq[Hash], b: IndexedSeq[Hash], node: Int = 0): Seq[Int] =
  if a(node) == b(node) then Seq.empty
  else if 2 * node + 1 >= a.size then Seq(node - a.size / 2)
  else diff(a, b, 2 * node + 1) ++ diff(a, b, 2 * node + 2)
```

The replicas exchange `tree(0)` first. Equal roots end the exchange in one round trip. Otherwise `diff` descends only mismatched branches and returns the buckets to reconcile; the nodes then transfer the keys in those buckets and use vector clocks — or, for convergent replicated data types, a merge function — to reconcile the versions.

## How deployed systems wire it up

**Cassandra and ScyllaDB.** `nodetool repair` runs anti-entropy. The initiating node acts as coordinator and triggers a **validation compaction** on each replica, which "reads and generates a hash for every row... adds the result to a Merkle tree." Cassandra uses a fixed depth of 15 — `2^15 = 32K` leaf nodes — so each leaf covers a **range of rows rather than a single row**. The documented consequence: for "a node containing a million partitions with one damaged partition, about 30 partitions are streamed." Coarse leaves over-stream, and the tree stays bounded in size.

```bash
# Repair one keyspace across its replicas
nodetool repair my_keyspace

# Restrict the repair to the ranges this node owns as primary
nodetool repair -pr my_keyspace
```

**Riak.** Active Anti-Entropy "relies on Merkle tree hash exchanges between nodes" and descends the tree level by level until it isolates the differing values. Riak stores **persistent, on-disk hash trees**, so a restarted node does not rebuild from scratch, and expires and regenerates them on a schedule — the documented default expiry is one week. Regeneration re-reads the underlying objects, so a tree that had absorbed a corrupted value stops agreeing with its peer.

This is the anti-entropy protocol van Steen & Tanenbaum describe in their consistency-and-replication chapter — periodic pairwise reconciliation — with the Merkle tree as the mechanism that keeps each pairwise exchange from costing a full dataset scan.

| Concern | Merkle-tree anti-entropy |
|---|---|
| Bytes when replicas agree | one root hash |
| Bytes when they differ | paths to the diverged leaves |
| Leaf granularity | per-key (Dynamo) or per-range (Cassandra depth 15) |
| Main cost | rebuilding trees when ranges shift |

## The granularity tension

Dynamo names the operational cost directly: "many key ranges change when a node joins or leaves the system thereby requiring the tree(s) to be recalculated." Finer leaves reduce over-streaming and increase the cost to build and hold the tree; coarser leaves invert the trade. Cassandra fixes depth at 15 and accepts the over-streaming; Riak persists trees so the build cost is not paid on every restart.

## Pitfalls

- **Mismatched tree shape reports total divergence.** If two replicas disagree on the leaf count or on the range boundaries, every root differs and the diff degrades into streaming the whole range. Shape is part of the protocol, not a local tuning parameter.
- **Order-dependent leaf hashing produces phantom differences.** Folding `(key, value)` pairs into a leaf in arrival order yields different hashes for identical contents, because replicas accept writes in different orders. The keys must be sorted before folding.
- **Trees built from cached hashes hide bit rot.** A tree whose leaves are read from a stored hash rather than from the value on disk agrees with its peer even after the underlying data is corrupted; this is why Riak clears and regenerates its persistent trees on a schedule.
- **Ring changes invalidate trees wholesale.** A node joining or leaving moves key ranges, and the trees for the affected ranges must be recalculated — so repair scheduled close behind a topology change pays the build cost in full.
- **Coarse leaves inflate the transfer, not the diff.** With depth 15, one damaged partition costs about 30 streamed partitions; the comparison stays logarithmic, but the reconciliation step ships every key in the offending bucket.
- **Anti-entropy detects divergence, it does not resolve it.** The tree identifies which buckets differ; deciding which version survives still requires vector clocks or a merge function, and a store without either has no defined outcome.
