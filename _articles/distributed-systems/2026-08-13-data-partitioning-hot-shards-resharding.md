---
title: "Data Partitioning: Hot Shards, Key Salting, and Resharding"
date: 2026-08-13
track: distributed-systems
summary: "Range vs hash vs directory partitioning and how to pick a partition key, what to do when one shard melts (salting, split-and-scatter, DynamoDB adaptive capacity), how real systems reshard (fixed partition counts, dynamic splitting, Vitess-style copy-and-cutover), and local vs global secondary indexes."
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

When a dataset or write load outgrows one machine, you partition (shard) it. The interview has four checkpoints: how keys map to partitions, how you pick the key, what you do when one partition melts, and how you move data when the shard count changes.

## Three ways to map keys to partitions

**Range partitioning** assigns contiguous key ranges to partitions (Bigtable/HBase tablets, DynamoDB sort-key ranges). Range scans are cheap — adjacent keys are colocated — but monotonic keys (timestamps, sequential IDs) aim every insert at the last partition: a permanently hot tail.

**Hash partitioning** routes by `hash(key)`, spreading load evenly and destroying range locality; scans become scatter-gather. Cassandra and DynamoDB hash the partition key; Citus hash-distributes rows by a distribution column and explicitly warns against distributing on timestamps for exactly the locality-vs-skew reason.

**Directory-based partitioning** keeps an explicit lookup table from key (or key bucket) to shard. Most flexible — you can move any tenant anywhere — but the directory is extra infrastructure on every request path. Notion is the canonical write-up: **480 logical shards mapped onto 32 physical Postgres instances**, partitioned by `workspace_id`; 480 was chosen for its many divisors so hosts could grow 32 → 40 → 48 by remapping whole logical shards, never resplitting rows.

## Picking the partition key

Three tests, in order:

1. **Cardinality and spread**: many distinct values, no value dominating. `user_id` passes; `country` fails (one value can be 30% of traffic).
2. **Access alignment**: your dominant query should touch one partition. Multi-tenant SaaS almost always wants `tenant_id` (Citus's primary recommendation), which also **co-locates** a tenant's rows across tables so joins stay node-local. Notion's `workspace_id` is the same decision.
3. **No built-in hotspots**: monotonic keys under range partitioning, or a compound key whose first component is low-cardinality, recreate the hot tail.

## Hot shards and the celebrity problem

Even a good key can't save you from Justin Bieber: one partition key whose traffic exceeds a single partition's capacity. DynamoDB makes the limits concrete — about **3,000 RCU and 1,000 WCU per physical partition** — and layers mitigations:

- **Burst capacity**: up to ~5 minutes of unused throughput retained to absorb spikes.
- **Adaptive capacity**: instantly shifts table throughput toward hot partitions, no configuration.
- **Split for heat**: persistently hot partitions are split in two, and the split point follows *traffic*, not the key-range midpoint — it can isolate a single hot item on its own partition. DynamoDB even detects when splitting won't help (a monotonically increasing sort key just moves the heat to the new partition) and declines.

When the platform doesn't do this for you, the app-level equivalents are:

- **Key salting (write sharding)**: turn one hot key into N sub-keys; writes pick a salt, reads fan out and merge. You're trading read cost for write spread — keep N small.

```text
SALTS = 8                                     # 1 hot key -> 8 sub-keys
write(key, val):  put(f"{key}#{rand(SALTS)}", val)
read(key):        merge(get(f"{key}#{i}") for i in range(SALTS))
```

- **Split-and-scatter**: detect the hot range and split just that partition (manual split-for-heat), scattering the pieces across nodes.
- **Cache in front**: a celebrity's profile is read-heavy and cache-friendly; absorb reads before they reach the shard.

## Resharding: changing the partition count

- **Fixed partition count**: create far more logical partitions than nodes up front (Notion's 480; Riak and Elasticsearch use the same trick) and rebalance by moving *whole partitions*. Simple and predictable; the cost is guessing the count up front, and Notion still needed a double-write + three-day backfill + verification migration (with five minutes of planned downtime) to get onto the scheme.
- **Dynamic splitting**: partitions split when too big or too hot (HBase regions, DynamoDB split-for-heat). No up-front guess; the trade is transient unavailability/movement at split time and unsplittable item collections if constraints (like DynamoDB LSIs) pin them.
- **Consistent hashing**: bound the fraction of keys that move when nodes join or leave — the [ring mechanics are covered here](/articles/distributed-systems/2026-07-25-consistent-hashing-ring); in interviews, just say "virtual nodes, 1/n of keys move" and move on.

Whatever the scheme, live resharding follows the Vitess shape: **copy** source-shard data to destination shards, let **replication catch up** with ongoing writes, **verify** source against destination, then **cut over** serving and drop the sources — expansion and merge both work this way, without stopping writes.

## Secondary indexes under partitioning

| | Local (document-partitioned) | Global (term-partitioned) |
|---|---|---|
| **Index lives** | On each partition, covering its own rows | Partitioned by the *indexed value* itself |
| **Write** | One partition, atomic with the row | Cross-partition, usually async (DynamoDB GSIs are eventually consistent) |
| **Read by indexed field** | Scatter-gather across all partitions | One index partition |
| **Hot-spot risk** | Reads amplify with partition count | A popular term makes a hot index partition |
| **Examples** | Cassandra secondary indexes, DynamoDB LSI | DynamoDB GSI, global indexes in Vitess/CockroachDB |

Rule of thumb: local indexes keep writes clean and taxes reads; global indexes make indexed reads cheap and push complexity (and eventual consistency) into writes. DynamoDB's LSI carries an extra operational footgun: its presence prevents adaptive capacity from splitting an item collection across partitions.

## Comparison table

| | Range | Hash | Directory |
|---|---|---|---|
| **Locality / scans** | Excellent | None (scatter-gather) | Whatever you design |
| **Skew risk** | High (monotonic keys) | Low, until a single hot key | Low — remap at will |
| **Resharding** | Split ranges | Consistent hashing or fixed partitions | Update the mapping |
| **Extra infra** | Range metadata | None | Lookup service (must be HA) |
| **Used by** | HBase/Bigtable, DynamoDB sort keys | Cassandra, DynamoDB, Citus | Notion, Vitess vindex lookups |

**Try next:** simulate the celebrity problem — hash 10 M synthetic events across 16 buckets with one key receiving 20% of traffic, plot per-bucket load, then re-run with an 8-way salt on that key and measure the p99 bucket load drop.
