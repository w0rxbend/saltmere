---
title: "Sharded counters: when one hot key melts a partition"
date: 2026-08-13
track: sys-patterns
summary: "A viral post's like counter is one row that every writer must serialize on — lock queues in SQL, a 1,000-WCU partition ceiling in DynamoDB, one overloaded tablet in Bigtable. The remedy is N sub-counters written at random and summed at read time, with probabilistic sketches and stream aggregation where exact-and-instant is not required."
reading_time: 7
tags: [sharded-counters, hot-keys, dynamodb, redis, write-scaling]
sources:
  - title: "AWS — Using write sharding to distribute workloads evenly (DynamoDB)"
    url: "https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-partition-key-sharding.html"
  - title: "AWS — Best practices for designing and using partition keys effectively (DynamoDB)"
    url: "https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-partition-key-design.html"
  - title: "Google Cloud — Bigtable schema design best practices (hotspotting)"
    url: "https://docs.cloud.google.com/bigtable/docs/schema-design"
  - title: "Redis — INCR command and counter/rate-limiter patterns"
    url: "https://redis.io/docs/latest/commands/incr/"
---

**Gist.** A single logical counter — likes on a post, views on a video — is stored as one row or one item, and every increment must serialize on that one physical location, so throughput is capped by a single lock queue, partition, or tablet no matter how large the cluster is. The remedy is to split the counter into **N sub-counters** that are incremented independently and summed on read, exploiting the fact that addition commutes and therefore needs no coordination between writers. The cost is paid by the reader and by storage: every read touches N keys instead of one, N must be chosen and later resized, and the total is only as fresh as the slowest shard read.

## Why one counter serializes

The bottleneck differs per store, but its shape does not: **all writes to a logical counter land on one physical location, and that location admits one writer at a time.**

**Relational rows.** Each `UPDATE counters SET n = n + 1 WHERE id = ?` acquires a row lock that is held until commit. Concurrent writers to the same row queue behind that lock, so sustained throughput is bounded by **1 / (lock hold time)** — a bound that does not improve with more cores, more connections or more replicas, because the constraint is the serial section, not the parallel capacity around it. In multi-version concurrency control (MVCC) engines such as PostgreSQL, each increment additionally writes a new row version, so a hot counter accumulates dead tuples and vacuum pressure on top of the queueing.

**DynamoDB.** Capacity is allocated per *partition*, not per table. [AWS's partition-key guidance](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-partition-key-design.html) documents a per-partition ceiling of 3,000 read capacity units per second and 1,000 write capacity units per second. A counter item has one partition key and therefore lives in one partition, so **a single-item counter is capped near 1,000 write capacity units per second and throttles beyond it**, whatever the table-level provisioning says. Adaptive capacity redistributes throughput across partitions of a skewed workload; it cannot subdivide one item.

**Bigtable and HBase.** A row is served by exactly one tablet on one node. [Google's schema-design guidance](https://docs.cloud.google.com/bigtable/docs/schema-design) warns against key shapes that concentrate traffic — sequential identifiers, raw timestamps at the front of the key — because they concentrate reads and writes on a single node rather than spreading them across the cluster.

Scaling the cluster out does not help, because the key itself pins the traffic to one shard. The quantity that must be scaled is the **key space of the counter**.

## The sharded counter

Split the logical counter `c` into sub-counters `c:0 … c:N-1`.

- **Write.** Increment one shard, selected uniformly at random, or by hashing the writer identifier.
- **Read.** Fetch all N shards and sum them.

The correctness argument is that **increments commute**: the sum over shards is invariant under any interleaving of increments, so writers need no coordination with one another. The single lock queue becomes N independent queues, and write throughput scales by approximately N until some other limit binds.

The invariant is weak but exact in the limit: **the sum equals the true count once every increment issued before the read has been applied to its shard.** A read that races concurrent increments returns a value between the count at read start and the count at read end — sub-counters are read at different instants, so the result is not a snapshot of any single moment. For a monotonically increasing counter this is a lower bound that is never stale by more than the in-flight writes; for a counter that also decrements, the read can transiently fall outside the true range in either direction.

In DynamoDB the same shape is documented as [write sharding](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-partition-key-sharding.html): the partition key becomes `counter#<id>#<suffix>` with suffix in `0..N-1`, the write is an `UpdateItem` with an `ADD` expression, and the read queries all suffixes and sums. AWS's worked example distributes a hot date key across suffixes 1–200. A **calculated** suffix — `hash(writer_id) mod N` — keeps a single-item lookup possible for a given writer; a **random** suffix maximizes spread but makes any individual item unaddressable without a scan of all suffixes.

**Choosing N.** The sizing rule is `N ≈ peak_writes_per_second / per_shard_capacity`, with headroom for skew in the random assignment. With the documented DynamoDB ceiling of 1,000 write units per second per partition, a counter sustaining 20,000 writes per second requires N ≥ 20, and doubling to 40 absorbs both burst and the imperfect balance of random placement. Overshooting is not free: **each read consumes read capacity proportional to N**, and every counter carries N items of storage and metadata whether or not it is hot. Growing N is cheap because new shards start at zero and the sum remains correct; shrinking N requires merging shard values under a scheme that cannot lose concurrent increments.

### Implementation sketch (Scala)

```scala
import java.util.concurrent.ThreadLocalRandom

/** Minimal contract a sharded counter needs from a store: commutative add, batch read. */
trait CounterStore:
  def add(key: String, delta: Long): Unit
  def getAll(keys: Seq[String]): Seq[Long]

final class ShardedCounter(store: CounterStore, shards: Int):
  require(shards > 0)

  private def key(id: String, shard: Int): String = s"cnt:$id:$shard"

  /** Random placement: no per-writer affinity, so no shard inherits a skewed writer. */
  def increment(id: String, delta: Long = 1L): Unit =
    store.add(key(id, ThreadLocalRandom.current().nextInt(shards)), delta)

  /** Sticky placement: the writer's own shard is addressable without reading all N. */
  def incrementSticky(id: String, writerId: String, delta: Long = 1L): Unit =
    store.add(key(id, Math.floorMod(writerId.hashCode, shards)), delta)

  /** Not a snapshot: shards are observed at different instants. */
  def read(id: String): Long =
    store.getAll((0 until shards).map(key(id, _))).sum

  /** Growing N keeps the sum correct because absent shards read as zero. */
  def widen(shards2: Int): ShardedCounter =
    require(shards2 >= shards)
    ShardedCounter(store, shards2)
```

The same decomposition appears in the JDK: `java.util.concurrent.atomic.LongAdder` maintains a table of cells that threads increment independently and sums in `sum()`, which likewise **is not an atomic snapshot** when increments are concurrent.

## When approximation suffices

Where the product requirement is "3.2M views", exactness is not being bought for anything. **Unique** counts — distinct viewers, daily active users — cannot be sharded and summed at all, because the same subject may appear in more than one shard and would be counted twice. [HyperLogLog sketches](/articles/distributed-systems/2026-08-10-hyperloglog-cardinality-estimation) solve this because **their merge is a union rather than a sum**: per-shard sketches (`PFADD`) combine with `PFMERGE` at read, at roughly 0.8% relative error in 12 KB. Count-min sketches provide the analogous merge for heavy-hitter frequency estimates. The distinction is the load-bearing one: sharding scales exact additive counts; sketches scale set-cardinality counts.

## Write-behind aggregation

The third lever removes increments from the hot path. Writers emit `+1` events to a log — Kafka partitioned by counter identifier, or a Redis Stream — and a consumer aggregates them in memory, flushing an `ADD delta` to the durable record on an interval. Two consequences follow directly. First, **the visible count lags by one flush interval**, which is acceptable for view counters and incorrect for account balances. Second, **the flush must be idempotent**, since a consumer crash between flush and offset commit otherwise replays a delta; storing the consumer offset transactionally alongside the aggregate closes this, and omitting it accepts bounded over- or under-counting proportional to the unflushed window. The pattern composes with sharding: aggregate per stream partition, sum at read.

## Choosing an approach

| Requirement | Mechanism |
|---|---|
| Exact, real-time, high write rate | Sharded counter, read = sum of N |
| Unique counts (viewers, daily actives) | HyperLogLog, merge at read |
| Exact eventually, very high rate | Stream plus write-behind aggregation |
| Rate limiting per key | Plain `INCR` with `EXPIRE`; seldom hot enough to shard |

## Pitfalls

- **Sharding a unique count.** Summing per-shard distinct counts double-counts every subject that appears on more than one shard; the correct primitive is a mergeable sketch whose combine operation is union.
- **Reading N shards on a cold counter.** Sharding every counter, rather than the measured hot ones, multiplies read capacity and storage by N across the entire key space for no write-throughput gain.
- **Shrinking N in place.** Reducing the shard count strands the values in the removed shards, and merging them while writers are still active loses increments that land on a shard between its final read and its deletion.
- **Treating the read as a snapshot.** Shards are sampled at different instants, so a monotonic counter can appear to move backwards across two reads issued from different clients, and any invariant of the form "count equals number of rows" fails under concurrency.
- **Sticky placement with skewed writers.** `hash(writer_id) mod N` concentrates traffic on one shard when a small number of writers produce most increments — a bot or a replay loop reproduces the original hot-key problem inside the sharded scheme.
- **Non-idempotent write-behind flush.** A consumer that flushes the aggregate and then commits its offset double-counts the window on crash; one that commits first loses it.
