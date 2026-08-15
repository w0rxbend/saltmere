---
title: "Partitioning and sharding: choosing a scheme and surviving the resize"
date: 2026-08-10
track: distributed-systems
summary: "A single node exhausts its disk and CPU, so the dataset is split. This article covers the three partitioning schemes and their trade-offs, the hot-key problem and salting, secondary indexes under partitioning, and why hash(key) % N reshuffles almost the whole cluster on resize."
reading_time: 7
tags: [partitioning, sharding, consistent-hashing, rebalancing, ddia]
sources:
  - title: "Kleppmann, Designing Data-Intensive Applications — Ch.6 Partitioning (O'Reilly)"
    url: "https://www.oreilly.com/library/view/designing-data-intensive-applications/9781491903063/"
  - title: "MongoDB Manual — Hashed Sharding"
    url: "https://www.mongodb.com/docs/manual/core/hashed-sharding/"
  - title: "Vitess Docs — Vindexes"
    url: "https://vitess.io/docs/22.0/reference/features/vindexes/"
  - title: "Citus Docs — Choosing the Distribution Column"
    url: "https://docs.citusdata.com/en/v11.1/sharding/data_modeling.html"
  - title: "Murat Demirbas, DDIA Ch.6 Partitioning (review)"
    url: "http://muratbuffalo.blogspot.com/2024/09/ddia-chp-6-partitioning.html"
---

**Gist.** A single node has finite disk, RAM and I/O operations per second (IOPS), so a dataset that outgrows it must be split into **partitions** (shards) spread across nodes. Every partitioning scheme is a choice of key-to-partition function, and each function trades even load distribution against the ability to answer a query from one partition. The cost is paid twice: at query time, when a scheme that scatters keys forces scatter-gather; and at resize time, when a scheme whose mapping depends on the node count moves nearly all the data.

The failure that partitioning exists to prevent, and the one it most often creates, is the **hot spot**: a partition — or the node hosting it — that receives a disproportionate share of traffic. A cluster with one saturated node and nine idle ones has the throughput of one node. Kleppmann's *Designing Data-Intensive Applications* (DDIA) Chapter 6 organises the topic as a sequence of decisions, and this article follows that order: which scheme, how to handle skew, what happens to secondary indexes, and how to add nodes without reshuffling everything.

## Three ways to assign keys to partitions

**Range partitioning** keeps keys sorted and gives each partition a contiguous range — `a`–`f` on one partition, `g`–`m` on the next. Because adjacent keys are stored together, a bounded scan such as `WHERE ts BETWEEN ...` is answered by **one partition**. HBase and Bigtable partition this way. The failure mode is skew under monotonically increasing keys: if the key is a timestamp and writes are always "now", every insert lands on **the single partition whose upper bound is the maximum**, producing a sequential-write hot spot even while the partitions remain evenly sized. Range partitioning suits time-series *reads* and is hostile to time-series *writes* on a raw timestamp key.

**Hash partitioning** applies a hash function to the key and assigns by the hash value, so `hash("now")` and `hash("now" + 1 ms)` land on unrelated partitions. This removes the sequential-write hot spot. Cassandra uses Murmur3; MongoDB's hashed shard key exists so that a monotonically increasing `_id` distributes across chunks rather than accumulating in one. The cost is the loss of sorted order: a range query must fan out to **every** partition (a broadcast, or scatter-gather), because neighbouring keys are deliberately scattered. The MongoDB manual states the trade directly — hashed sharding gives even distribution in exchange for targeted range operations.

**Directory (lookup-based) partitioning** stores an explicit map from key, or key-range, to partition in a routing tier. Vitess *lookup vindexes* implement this: a backing table holds `column value -> keyspace ID`, which permits sharding on a secondary column or deliberately placing specific keys. It is the most flexible scheme — relocating one hot tenant to a dedicated shard is an edit to a single mapping — but the directory is an extra hop, a shared dependency and a single point of failure unless replicated and cached.

| Scheme | Even spread | Range scans | Move one hot key | Real systems |
|---|---|---|---|---|
| Range | Poor (skew/append hot spots) | Excellent | Split the range | HBase, Bigtable |
| Hash | Excellent | Broadcast to all | Hard (see salting) | Cassandra, MongoDB hashed, Citus |
| Directory/lookup | Fully controllable | Depends on backing | Trivial (edit map) | Vitess lookup vindex |

## The hot-key problem

Hashing distributes *keys* evenly; it cannot help when a single *key* is hot. An account with millions of followers, or one `product_id` during a flash sale, hashes to exactly one partition, and that partition is overloaded under any scheme. DDIA notes that most systems do not correct this automatically: the responsibility sits with the application.

The standard mitigation is **salting**: the hot key is split into `N` sub-keys by prefixing or suffixing a small random value, so its writes spread over `N` partitions. The trade-off DDIA states is that **every read of a salted key must query all `N` sub-partitions and merge the results**, and the application must record which keys are salted — so salting is applied only to the few keys observed to be hot. The heavier alternative is a **dedicated shard** for the key, which a directory scheme makes cheap because rerouting is one map entry.

## Secondary indexes: local versus global

Partitioning by primary key does not carry secondary indexes (`WHERE color = 'red'`) along with it. Two designs exist.

- **Local, or document-partitioned, index.** Each partition indexes only the rows it stores. Writes touch **one partition** and can be applied atomically with the row. Reads on the indexed attribute must **scatter-gather across every partition**, because matching rows exist everywhere. Elasticsearch, MongoDB and Cassandra's default indexes work this way.
- **Global, or term-partitioned, index.** The index is itself partitioned by the *term*: `color=red` on one partition, `color=blue` on another. Reads are targeted to one partition. Writes are cross-partition — one row insert may touch several index partitions — and are therefore commonly applied **asynchronously**, so the index lags the data.

Local partitioning favours writes, global favours reads, and the asynchronous update path of a global index is why a freshly written row can be absent from an index query that follows it.

## Rebalancing: why `hash(key) % N` is a trap

The direct hash mapping is `node = hash(key) % N`. It is balanced while `N` is constant, but `N` is the divisor: when a node is added, almost every key's residue changes, and nearly the whole dataset must move at once. A key keeps its node across a move from 5 nodes to 6 only when `hash(key) % 5 == hash(key) % 6`, which for uniformly distributed hashes holds for about one key in six; the other **roughly five keys in six move**, where the ideal for adding a sixth node is about 1/6. The consequences are a network storm during the move and a cold cache on every node, not only the new one.

**Fix 1 — a fixed number of partitions.** Decouple partition count from node count: create many partitions up front, far more than there are nodes, and assign whole partitions to nodes. With `partition = hash(key) % 1024`, the divisor never changes, so no key ever rehashes; adding a node moves **whole partitions**, not keys. Citus (32 shards by default, assigned by hash range) and Elasticsearch work this way. The constraint is that the partition count is fixed at creation and bounds how far the data can be spread.

**Fix 2 — consistent hashing.** Nodes and keys are placed on a hash ring and a key belongs to the next node clockwise, so adding a node reassigns only the arc between it and its predecessor — **about K/N keys**, the theoretical minimum for K keys over N nodes. Dynamo and Cassandra use this. A ring walkthrough is at [/articles/distributed-systems/2026-07-25-consistent-hashing-ring](/articles/distributed-systems/2026-07-25-consistent-hashing-ring), and [rendezvous (highest random weight, HRW) hashing](/articles/distributed-systems/2026-08-10-rendezvous-hrw-hashing) achieves the same K/N churn without ring bookkeeping.

**Fix 3 — dynamic partitioning.** In range-partitioned stores, a partition **splits** when it grows past a threshold and **merges** when it shrinks, as a B-tree node does. HBase and MongoDB do this, so the partition count tracks data volume instead of being fixed in advance.

### Implementation sketch (Scala)

The two mappings differ in one place — whether the node count appears in the key's hash — and that difference is what the churn measurement exposes.

```scala
final case class Ring(nodes: Vector[String]):
  // Modulo mapping: the node count is the divisor, so it enters every key's result.
  def byModulo(key: String): String =
    nodes(Math.floorMod(key.hashCode, nodes.size))

  // Fixed-partition mapping: the divisor is a constant, so keys never rehash.
  def byPartition(key: String, partitions: Int = 1024): String =
    val p = Math.floorMod(key.hashCode, partitions)
    nodes(p * nodes.size / partitions)   // whole partitions assigned to nodes

def churn(keys: Seq[String], before: Ring, after: Ring)(
    owner: (Ring, String) => String): Double =
  keys.count(k => owner(before, k) != owner(after, k)).toDouble / keys.size

val keys  = (1 to 100_000).map(i => s"key:$i")
val five  = Ring((1 to 5).map(i => s"n$i").toVector)
val six   = Ring((1 to 6).map(i => s"n$i").toVector)

churn(keys, five, six)((r, k) => r.byModulo(k))     // near-total reshuffle
churn(keys, five, six)((r, k) => r.byPartition(k))  // only reassigned partitions move
```

## Request routing

Once keys move, clients must locate them. Three arrangements appear in production systems.

1. **Client-aware.** The client holds the partition map and connects to the owning node directly: one network hop, at the cost of every client tracking topology changes.
2. **Routing tier.** A partition-aware proxy — Vitess `vtgate`, MongoDB `mongos` — forwards requests. Clients stay simple; the system gains a hop and a tier to operate.
3. **Any-node forwarding.** A request goes to any node, which forwards it if it does not own the key. Cassandra and Riak gossip membership so that every node can route. No separate tier, at the cost of membership traffic.

The routing table is itself distributed state, so the authoritative map is often held in a consensus store such as ZooKeeper or etcd, with changes pushed to subscribers.

## Pitfalls

- **A timestamp primary key under range partitioning serialises all writes.** Every insert falls in the highest range, so one node absorbs the entire write rate while the others sit idle and the partition sizes still look balanced.
- **Hash partitioning silently converts range queries into broadcasts.** A `BETWEEN` filter that was one seek before sharding now touches every partition, and tail latency becomes the slowest partition's latency.
- **Setting the partition count equal to the node count.** Every subsequent capacity change alters the divisor, so nearly all keys are remapped and moved at once.
- **Salting a key without recording that it is salted.** Reads that use the unsalted key return only the fraction of writes that happened to carry no prefix, which reads as data loss rather than as a routing bug.
- **Reading a global secondary index immediately after a write.** The index is updated asynchronously across partitions, so the row exists and the index entry does not yet.
- **Fixing the partition count too low at creation.** Partitions cannot be subdivided later in this scheme, so the maximum useful node count is capped at the partition count.
- **Treating a lookup directory as free.** It is consulted on every routed request; if it is neither cached nor replicated, its availability becomes the cluster's availability.
