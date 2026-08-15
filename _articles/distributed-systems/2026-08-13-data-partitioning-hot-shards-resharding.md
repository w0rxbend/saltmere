---
title: "Data Partitioning: Hot Shards, Key Salting, and Resharding"
date: 2026-08-13
track: distributed-systems
summary: "Range, hash and directory partitioning and the criteria for a partition key; mitigations when one shard melts (salting, split-and-scatter, DynamoDB adaptive capacity); resharding schemes (fixed partition counts, dynamic splitting, Vitess-style copy-and-cutover); and local vs global secondary indexes."
reading_time: 6
tags: [sharding, partitioning, hot-keys, resharding, secondary-indexes]
sources:
  - title: "Burst and Adaptive Capacity (Amazon DynamoDB Developer Guide)"
    url: "https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/burst-adaptive-capacity.html"
  - title: "Scaling DynamoDB: How Partitions, Hot Keys, and Split for Heat Impact Performance (AWS Database Blog)"
    url: "https://aws.amazon.com/blogs/database/part-2-scaling-dynamodb-how-partitions-hot-keys-and-split-for-heat-impact-performance/"
  - title: "What Is Resharding? How Does It Work? (Vitess Docs)"
    url: "https://vitess.io/docs/faq/sharding/overview/what-is-resharding-how-does-it-work/"
  - title: "Choosing Distribution Column (Citus Documentation)"
    url: "https://docs.citusdata.com/en/stable/sharding/data_modeling.html"
  - title: "Herding Elephants: Lessons Learned from Sharding Postgres at Notion (Notion Engineering)"
    url: "https://www.notion.com/blog/sharding-postgres-at-notion"
---

**Gist.** When a dataset or its write load exceeds one machine, it is partitioned (sharded) across many, and the mapping from key to partition decides both locality and skew. The mechanisms in use are range assignment, hash assignment and an explicit directory, each combined with a rebalancing scheme that moves data when the partition count changes. The cost is paid in three places: cross-partition reads when the access pattern does not align with the key, residual hot partitions when one key dominates, and a copy-catch-up-verify-cutover migration every time the layout changes.

## Three ways to map keys to partitions

**Range partitioning** assigns contiguous key ranges to partitions (Bigtable and HBase tablets, DynamoDB sort-key ranges). Range scans are cheap because adjacent keys are colocated. The failure mode is structural: a **monotonic key** — a timestamp or a sequential identifier — always sorts into the highest range, so every insert lands on the last partition, producing a permanently hot tail that adding partitions does not relieve.

**Hash partitioning** routes by `hash(key)`. It spreads load evenly and destroys range locality, so any scan over a key interval becomes scatter-gather across all partitions. Cassandra and DynamoDB hash the partition key; Citus hash-distributes rows by a distribution column, and its documentation states plainly, "Do not choose a timestamp as the distribution column" — because hashing scatters adjacent times across shards, so a query over a time range touches all of them.

**Directory-based partitioning** keeps an explicit lookup table from key, or from a key bucket, to shard. It is the most flexible arrangement — any tenant can be relocated by editing one row — at the cost of an extra service on every request path, which must itself be highly available. Notion's account is the reference write-up: **480 logical shards mapped onto 32 physical PostgreSQL instances**, partitioned by workspace ID, 15 logical shards to an instance. The write-up gives the reason for the count directly: "480 is divisible by a lot of numbers — which provides flexibility to add or remove physical hosts while preserving uniform shard distribution." A power-of-two count would instead force the host count to double.

## Selecting the partition key

Three tests, applied in order.

1. **Cardinality and spread.** Many distinct values, none dominating. `user_id` satisfies this; `country` does not, since a single value can carry a large share of traffic.
2. **Access alignment.** The dominant query should touch one partition. Multi-tenant applications generally partition on `tenant_id`, which is Citus's primary recommendation, because it also **co-locates** a tenant's rows across tables so joins remain node-local. Notion's `workspace_id` is the same decision.
3. **Absence of built-in hotspots.** A monotonic key under range partitioning, or a compound key whose leading component has low cardinality, reintroduces the hot tail regardless of the remaining components.

## Hot shards and the celebrity problem

A well-chosen key does not defend against a single key whose traffic exceeds one partition's capacity. DynamoDB documents the per-partition limits — throttling begins above **3,000 read operations or 1,000 write operations per second on a single partition** — and layers three mitigations:

- **Burst capacity**: DynamoDB "currently retains up to five minutes (300 seconds) of unused read and write capacity", consumable during a spike. The documentation notes these details might change.
- **Adaptive capacity**: throughput is raised for partitions receiving more traffic, automatically and at no cost, **bounded by the table's total provisioned capacity and by the partition maximum**. Under consistently high traffic to one item, the documented behaviour is that DynamoDB may rebalance until that partition holds only that item, which raises its ceiling to the partition maximum of 3,000 RCU and 1,000 WCU but no further.
- **Split for heat**: a persistently hot partition is divided, and **the split point is calculated from recent traffic patterns rather than taken as the midpoint**. Two documented cases where the split is declined or constrained: a monotonically increasing sort key, since the writes after the split would all land on the second partition anyway; and a table carrying a local secondary index (LSI), where the split point can fall only between item collections.

Where the platform provides no such mechanism, the application-level equivalents are:

- **Key salting, also called write sharding**: one hot key becomes N sub-keys; a write picks a salt, and a read fans out over all N and merges. This exchanges read cost for write spread, and **read amplification is exactly N**, so N stays small.
- **Split-and-scatter**: detect the hot range, split that partition alone, and distribute the pieces across nodes — the manual form of split-for-heat.
- **Caching in front**: a celebrity record is read-heavy and cacheable, so reads are absorbed before reaching the shard.

### Implementation sketch (Scala)

The load-bearing property of salting is that the write path is O(1) and the read path is O(N); nothing about the storage engine changes.

```scala
final case class Salted(fanout: Int):
  require(fanout >= 1)

  /** Writes go to one randomly chosen sub-key, so write cost stays O(1). */
  def writeKey(key: String): String =
    s"$key#${scala.util.Random.nextInt(fanout)}"

  /** Reads must visit every sub-key: read amplification is exactly `fanout`. */
  def readKeys(key: String): Vector[String] =
    Vector.tabulate(fanout)(i => s"$key#$i")

// Counter example: the merge is a fold, so the sub-values must be commutative.
final class SaltedCounter(
    store: scala.collection.concurrent.Map[String, java.util.concurrent.atomic.AtomicLong],
    s: Salted):

  // getOrElseUpdate on a concurrent Map is atomic; a read-modify-write on a
  // plain `Long` value would lose concurrent increments to the same sub-key.
  def increment(key: String, by: Long): Unit =
    store.getOrElseUpdate(s.writeKey(key), java.util.concurrent.atomic.AtomicLong(0L)).addAndGet(by)

  def total(key: String): Long =
    s.readKeys(key).foldLeft(0L)((acc, k) => acc + store.get(k).fold(0L)(_.get()))
```

Only values whose merge is associative and commutative — counters, sets, append-only lists — survive this transformation unchanged. A value read for compare-and-set does not, because the salt destroys the single point of serialization. The total is a fold over sub-keys that are updated independently, so it is not a point-in-time snapshot: increments landing on an already-visited sub-key during the fan-out are missed.

## Resharding: changing the partition count

- **Fixed partition count.** Far more logical partitions than nodes are created up front (Notion's 480; Riak and Elasticsearch use the same arrangement) and rebalancing moves *whole partitions*. The behaviour is predictable, and the cost is the up-front estimate of the count. Reaching the scheme is itself a migration: Notion reports double-writing, a three-day backfill, verification, and five minutes of planned downtime.
- **Dynamic splitting.** Partitions split when they grow too large or too hot (HBase regions, DynamoDB split-for-heat). No estimate is required up front; the cost is movement and transient unavailability at split time, plus item collections that cannot be split when an LSI pins them.
- **Consistent hashing.** The fraction of keys that move when a node joins or leaves is bounded; the [ring mechanics are covered here](/articles/distributed-systems/2026-07-25-consistent-hashing-ring).

Live resharding follows the shape Vitess documents: **copy** source-shard data to the destination shards, allow **replication to catch up** with writes that arrived during the copy, **verify** source against destination, then **cut over** serving traffic and delete the sources. Vitess documents both directions this way: splitting shards into smaller pieces, and merging neighbouring shards into bigger ones.

## Secondary indexes under partitioning

| | Local (document-partitioned) | Global (term-partitioned) |
|---|---|---|
| **Index lives** | On each partition, covering its own rows | Partitioned by the *indexed value* itself |
| **Write** | One partition, atomic with the row | Cross-partition, usually asynchronous (DynamoDB global secondary indexes are eventually consistent) |
| **Read by indexed field** | Scatter-gather across all partitions | One index partition |
| **Hot-spot risk** | Reads amplify with partition count | A popular term produces a hot index partition |
| **Examples** | Cassandra secondary indexes, DynamoDB LSI | DynamoDB global secondary index (GSI), Vitess lookup vindexes |

A local index keeps the write path atomic and taxes the read path in proportion to the partition count; a global index makes the indexed read a single-partition operation and moves the cost, and the eventual consistency, into the write path. The DynamoDB LSI carries a further operational consequence: its presence prevents an item collection from being split across partitions, so the adaptive mechanism above cannot relieve heat on that collection.

## Comparison

| | Range | Hash | Directory |
|---|---|---|---|
| **Locality / scans** | Preserved | None (scatter-gather) | Determined by the mapping |
| **Skew risk** | High (monotonic keys) | Low, until a single hot key | Low — remap at will |
| **Resharding** | Split ranges | Consistent hashing or fixed partitions | Update the mapping |
| **Extra infrastructure** | Range metadata | None | Lookup service (must be highly available) |
| **Used by** | HBase, Bigtable, DynamoDB sort keys | Cassandra, DynamoDB, Citus | Notion, Vitess vindex lookups |

## Pitfalls

- **A timestamp as the leading component of a range-partitioned key** sends every insert to the last partition; the remaining components never influence placement, so the hot tail persists at any partition count.
- **Salting a value that is read-modify-written** loses the single serialization point: two concurrent updates landing on different salts cannot be reconciled by the merge, and one is lost.
- **Choosing the salt fan-out N too large** makes every read of that key an N-way fan-out, converting a write hotspot into a latency problem on the read path.
- **Attaching a local secondary index in DynamoDB** pins the item collection to one partition, so a hot collection cannot be relieved by split-for-heat.
- **Assuming adaptive capacity or split-for-heat resolves a monotonically increasing sort key**: the documented behaviour is that the split is declined, because the heat would move to the new partition rather than be divided.
- **Under-provisioning the logical partition count** in a fixed-count scheme forces a full re-partition of rows later, which is the migration the scheme exists to avoid.
- **Treating the directory service as incidental** places a mandatory lookup on every request path; its unavailability is total unavailability of the data tier.
