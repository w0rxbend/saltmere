---
title: "Partitioning and sharding: pick a scheme, then survive the resize"
date: 2026-08-10
track: distributed-systems
summary: "A single node runs out of disk and CPU, so you split the data. This walks the three partitioning schemes and their trade-offs, the hot-key problem and how salting fixes it, secondary indexes under partitioning, and the one decision that bites everyone in interviews: why hash(key) % N melts your cluster on resize, and what to do instead."
reading_time: 6
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

One node has a finite amount of disk, RAM, and IOPS. When your dataset or write rate outgrows it, you split the data into **partitions** (shards) and spread them across nodes. The goal is to spread load *evenly*: a partition holding 90% of the traffic, or the node it lives on, is a "hot spot," and a hot spot means one machine is your bottleneck no matter how many others you bought. Kleppmann's DDIA Ch.6 frames the whole topic as a sequence of decisions, and this article walks them in interview order: which scheme, how to handle skew, what happens to secondary indexes, and — the part people botch — how to add nodes without reshuffling everything.

## Three ways to assign keys to partitions

**Range partitioning** keeps keys sorted and gives each partition a contiguous range — `a`–`f` here, `g`–`m` there, like volumes of an encyclopedia. Range scans are cheap because adjacent keys sit together, so `WHERE ts BETWEEN ...` hits one partition. HBase and Bigtable work this way. The danger is skew: if your key is a timestamp and you're writing "now," every insert lands on the *same* partition (the one whose upper bound is the max), so you get a sequential-write hot spot even though the data is evenly sized. Range is great for time-series *reads*, terrible for time-series *writes* on a raw timestamp key.

**Hash partitioning** runs the key through a hash function and assigns by the hash value, so `hash("now")` and `hash("now"+1ms)` land on different partitions. This destroys the sequential-write hot spot and spreads load evenly — Cassandra uses Murmur3, MongoDB's hashed shard key exists specifically so a monotonically increasing `_id` distributes instead of piling onto one chunk. The cost is that you lose sorted order: a range query now has to fan out to *every* partition (a broadcast/scatter-gather), because "close" keys are deliberately scattered. MongoDB's docs say this plainly — hashed sharding trades targeted range operations for even distribution.

**Directory / lookup-based** partitioning keeps an explicit map from key (or key-range) to partition in a routing tier. Vitess's *lookup vindexes* do exactly this: a backing table stores `column value -> keyspace ID`, letting you shard on a secondary column or place specific keys deliberately. It's the most flexible — you can move a single hot tenant to its own shard by editing one mapping — but the directory is another hop and a potential bottleneck/SPOF, so it's usually cached and made highly available.

| Scheme | Even spread | Range scans | Move one hot key | Real systems |
|---|---|---|---|---|
| Range | Poor (skew/append hot spots) | Excellent | Split the range | HBase, Bigtable, Vitess range vindex |
| Hash | Excellent | Broadcast to all | Hard (see salting) | Cassandra, MongoDB hashed, Citus |
| Directory/lookup | Fully controllable | Depends on backing | Trivial (edit map) | Vitess lookup vindex |

## The hot-key (celebrity) problem

Hashing spreads *keys* evenly, but it can't help when one *key* is hot. A celebrity with 50M followers, or a `product_id` in a flash sale, hashes to exactly one partition — and now that partition is overloaded regardless of scheme. DDIA notes most systems don't solve this automatically; it's on the application.

The standard fix is **salting**: split the hot key into `N` sub-keys by prefixing (or suffixing) a small random number, spreading its load across `N` partitions.

```python
# A hot key "celebrity:123" is split across 16 virtual sub-keys.
HOT_KEYS = {"celebrity:123"}
FANOUT = 16

def write_key(key):
    if key in HOT_KEYS:
        return f"{random.randrange(FANOUT)}:{key}"  # 0:celebrity:123 ... 15:celebrity:123
    return key

def read_all(key):
    if key in HOT_KEYS:
        # reads now scatter/gather across all 16 sub-keys and merge
        return merge(get(f"{s}:{key}") for s in range(FANOUT))
    return get(key)
```

The trade-off is explicit in DDIA: writes get distributed, but every *read* of a salted key must query all `N` sub-partitions and combine results, and you need bookkeeping to remember which keys are salted. So you salt only the few keys that are actually hot. The heavier-weight alternative is a **dedicated shard**: pull the celebrity onto its own node entirely — this is where a directory scheme shines, since rerouting one key is a one-line map edit.

## Secondary indexes: local vs global

Partition by primary key and your secondary indexes (`WHERE color = 'red'`) don't automatically follow. Two designs, and interviewers love the trade-off:

- **Local / document-partitioned index.** Each partition indexes only its own rows. Writes are cheap (one partition, atomic with the row). Reads are expensive: a query on `color` must **scatter-gather** across every partition because red cars live everywhere. This is Elasticsearch, MongoDB, Cassandra's default.
- **Global / term-partitioned index.** The index itself is partitioned by the *term* (`color=red` lives on one partition, `color=blue` on another). Reads are cheap and targeted. Writes are expensive and cross-partition: inserting one row may touch several index partitions, so it's usually done **asynchronously**, meaning your index can lag the data.

The rule of thumb: local favors writes, global favors reads, and global's async update is why "I wrote it but the index doesn't show it yet" is normal.

## Rebalancing: why `hash(key) % N` is a trap

Here's the mistake that sinks interviews. The obvious hash mapping is `node = hash(key) % N`. It's balanced — until `N` changes. Add one node and the *divisor* changes, so almost every key's `% N` result changes, and nearly all your data must move at once.

```python
keys = range(100_000)
def owner(k, N): return hash(k) % N
moved = sum(owner(k, 5) != owner(k, 6) for k in keys)
print(moved / 100_000)   # ~0.83 — going 5 -> 6 nodes reshuffles ~83% of keys
```

Adding 20% capacity should move ~1/6 of the data; `% N` moves 83% of it. That's a network storm and a cache wipeout for one node.

**Fix 1 — fixed number of partitions.** Decouple partition count from node count: create *many* partitions up front (say 1024), far more than nodes, and assign whole partitions to nodes. `partition = hash(key) % 1024` never changes because 1024 never changes. Adding a node just *moves a few whole partitions* onto it; keys never rehash. This is Citus (32 shards by default, hash-range assigned) and Elasticsearch. The catch: you fix the partition count at creation and it caps how far you can spread.

**Fix 2 — consistent hashing.** Place nodes and keys on a hash ring; a key belongs to the next node clockwise, so adding a node reassigns only the arc between it and its predecessor — about **K/N keys**, the theoretical minimum. Dynamo and Cassandra use this. See the ring walkthrough at [/articles/distributed-systems/2026-07-25-consistent-hashing-ring](/articles/distributed-systems/2026-07-25-consistent-hashing-ring), and [rendezvous (HRW) hashing](/articles/distributed-systems/2026-08-10-rendezvous-hrw-hashing) for a bookkeeping-free variant with the same K/N churn.

**Fix 3 — dynamic partitioning.** For range-partitioned stores, let partitions **split** when they grow past a threshold and **merge** when they shrink, like a B-tree. HBase and MongoDB do this; the partition count tracks the data volume automatically instead of being fixed.

## Request routing

Once keys move, clients need to find them. Three options, mirrored in real systems:

1. **Client-aware.** The client holds the partition map and connects directly — one hop, but every client must track topology changes.
2. **Routing tier.** A partition-aware proxy (Vitess's `vtgate`, a `mongos`) sits in front and forwards. Clean clients, extra hop and infrastructure.
3. **Gossip / any-node.** Contact any node; if it doesn't own the key it forwards. Cassandra and Riak gossip membership so every node can route. No separate tier, but more coordination chatter.

The routing table is itself distributed state, so many systems park the authoritative map in ZooKeeper/etcd and push changes to subscribers.

The interview through-line: **choose a scheme for your access pattern (range for scans, hash for even writes, directory for control), plan for skew before it bites, and never let your partition count equal your node count.** Fixed partitions or a hash ring is the difference between adding a node in seconds and reshuffling the whole cluster.

**Try next:** take the `hash(key) % N` snippet above, swap in the fixed-partition scheme (`hash % 1024`, then map partitions to nodes), and measure how many keys move going 5 → 6 nodes. You should see ~1/6, not 83%.
