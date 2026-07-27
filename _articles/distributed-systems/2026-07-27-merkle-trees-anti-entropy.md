---
title: "Merkle Trees: Reconciling Replicas Without Shipping the Data"
date: 2026-07-27
track: distributed-systems
summary: "How Dynamo-style key-value stores use hash trees to find the handful of keys that diverged between replicas, instead of comparing the whole dataset key by key."
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

Quorum reads with `R + W > N` catch divergence *when you read a key*. But replicas also drift silently: a node was down during a write, a hint expired, a disk bit-rotted. The keys nobody reads never get repaired by read-repair alone. That is the job of **anti-entropy** — a background protocol that reconciles replicas of the same key range — and the classic way to make it cheap is a **Merkle tree**.

The naive alternative is brutal. To confirm two replicas of a range holding a million keys agree, you either ship a million (key, hash) pairs across the network, or stream the whole range. Merkle trees turn that into a logarithmic conversation: if two replicas are identical, they exchange exactly one hash and stop.

## The structure

A Merkle tree is a hash tree. In Dynamo's words, "leaves are hashes of the values of individual keys. Parent nodes higher in the tree are hashes of their respective children" (§4.7). The root therefore commits to the entire dataset in a single value.

The comparison protocol falls straight out of that:

- If the two **roots** match, every leaf underneath matches — the replicas are in sync, done.
- If the roots differ, at least one leaf differs. Recurse into the children whose hashes disagree; prune the subtrees whose hashes agree.

You pay bandwidth only along the paths to the keys that actually diverged. One bad key in a million costs you a walk of depth `log2(leaves)`, not a million comparisons.

Dynamo keeps "a separate Merkle tree for each key range (the set of keys covered by a virtual node)," so two nodes that share a range can compare just that range's tree. (Assumes consistent hashing to define ranges and vector clocks to decide which divergent version wins — both covered in earlier articles here.)

## Building and diffing one

A minimal build-over-a-range and diff, with fixed leaf buckets so both replicas agree on tree shape:

```python
import hashlib

def h(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()

def bucket(key: str, num_leaves: int) -> int:
    # stable placement so both replicas build the same-shaped tree
    return int.from_bytes(h(key.encode())[:4], "big") % num_leaves

def build(kv: dict[str, str], num_leaves: int = 1024) -> list[bytes]:
    # leaves[i] = hash of all (key,value) pairs landing in bucket i
    leaves = [b""] * num_leaves
    for k in sorted(kv):                       # sorted => order-independent
        i = bucket(k, num_leaves)
        leaves[i] = h(leaves[i] + k.encode() + b"=" + kv[k].encode())
    # build parents bottom-up into a flat heap-style array
    level, tree = leaves, list(leaves)
    while len(level) > 1:
        level = [h(level[i] + level[i + 1]) for i in range(0, len(level), 2)]
        tree = level + tree
    return tree                                # tree[0] is the root

def diff(a: list[bytes], b: list[bytes], node: int = 0) -> list[int]:
    """Return leaf indices that differ, walking only mismatched subtrees."""
    if a[node] == b[node]:
        return []                              # whole subtree agrees -> prune
    left, right = 2 * node + 1, 2 * node + 2
    if left >= len(a):                         # reached a leaf
        return [node - (len(a) // 2)]          # map back to leaf index
    return diff(a, b, left) + diff(a, b, right)
```

Two replicas exchange `tree[0]` first. Equal roots end the exchange in one round trip. Otherwise `diff` descends only the mismatched branches and hands back the buckets to reconcile; the nodes then swap just those keys and let vector clocks (or CRDT merges) settle the winners.

## How real systems wire it up

**Cassandra / ScyllaDB.** `nodetool repair` runs anti-entropy. The initiating node becomes coordinator and triggers a **validation compaction** on each replica, which "reads and generates a hash for every row... adds the result to a Merkle tree." Cassandra uses a fixed depth of 15 — `2^15 = 32K` leaf nodes — so each leaf covers a *range* of rows rather than one row. The docs give the practical consequence: for "a node containing a million partitions with one damaged partition, about 30 partitions are streamed." Coarse leaves mean you over-stream a little, but the tree stays small.

```bash
# Full repair of one keyspace across replicas
nodetool repair my_keyspace

# Incremental, primary-range-only repair — the routine maintenance shape
nodetool repair -pr my_keyspace
```

**Riak.** Active Anti-Entropy "relies on Merkle tree hash exchanges between nodes" and "recursively compares the tree, level by level, until it pinpoints exact values with a difference." Notably Riak keeps *persistent, on-disk* hash trees so nodes can restart without rebuilding, and periodically clears and regenerates them (default: weekly) to catch silent on-disk corruption.

This is the "anti-entropy protocol" van Steen & Tanenbaum describe in their consistency-and-replication chapter — periodic pairwise reconciliation — with a Merkle tree as the mechanism that keeps each pairwise exchange from costing a full dataset scan.

| Concern | Merkle-tree anti-entropy |
|---|---|
| Bytes when replicas agree | one root hash |
| Bytes when they differ | ~ paths to diverged leaves |
| Leaf granularity | per-key (Dynamo) or per-range (Cassandra depth-15) |
| Main cost | rebuilding trees when ranges shift |

## The sharp edge

Dynamo names the real operational pain: "many key ranges change when a node joins or leaves the system thereby requiring the tree(s) to be recalculated." Rebuild cost is why Cassandra caps tree depth (bounded memory, some over-streaming) and why Riak persists trees to disk. The finer your leaves, the less you over-stream but the more the tree costs to build and hold — that granularity knob is the whole design tension.

**Try next:** Extend the snippet with a second dict that differs in exactly one key, print the leaf indices `diff` returns, then drop `num_leaves` from 1024 to 16 and watch how many extra keys you'd have to ship — that gap is Cassandra's "30 partitions for one bad row" in miniature.
