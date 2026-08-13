---
title: "Sharded counters: what to do when one hot key melts a partition"
date: 2026-08-13
track: sys-patterns
summary: "A viral post's like counter is one row that every writer must serialize on — lock queues in SQL, a 1,000-WCU partition ceiling in DynamoDB, one overloaded tablet in Bigtable. The fix is N sub-counters written at random and summed at read time, with probabilistic sketches and stream aggregation for when exact-and-instant isn't required."
reading_time: 5
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

A like button, a view counter, a rate limiter: `UPDATE counters SET n = n + 1 WHERE id = ?`. Correct, and fine — until one id goes viral and 50,000 writers per second all want the same row. Hot counters are the canonical **hot key** problem, and "how would you count likes on a viral post" is a system-design interview classic.

## Why one counter melts

The bottleneck is different per store, but it's always *serialization on a single physical location*.

**Relational rows:** every `UPDATE` takes a row lock held until commit. Writers to the same row form a lock queue, so throughput is bounded by `1 / (lock hold time)` regardless of how many cores or replicas you add. In MVCC engines like Postgres each increment also writes a new row version, so a hot counter generates dead-tuple bloat and vacuum pressure on top of the queueing.

**DynamoDB:** capacity is per *partition*, not per table. [AWS's own numbers](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-partition-key-design.html): "Every partition in a DynamoDB table is designed to deliver a maximum capacity of 3,000 read units per second and 1,000 write units per second." One counter item lives in one partition, so one hot key caps at ~1,000 writes/s and throttles — no matter what you've provisioned at the table level (adaptive capacity helps a *skewed* workload, not a single-item one).

**Bigtable/HBase:** a row lives on exactly one tablet served by one node, so [Google's schema-design guidance](https://docs.cloud.google.com/bigtable/docs/schema-design) is blunt about hotspotting: avoid keys that concentrate traffic (sequential IDs, raw timestamps at the key front) because "reads and writes tend to concentrate on a single node instead of being distributed evenly."

Scaling *out* doesn't help because the key pins you to one shard. The fix is to scale the *key*.

## The sharded counter

Split logical counter `c` into N sub-counters `c:0 … c:N-1`. **Write:** increment one shard chosen uniformly at random (or by hashing the writer id). **Read:** fetch all N shards and sum. Increments commute, so no coordination is needed — you've turned one lock queue into N parallel ones, multiplying write throughput by ~N at the cost of an N-key read.

```python
import random, redis
r = redis.Redis()
N = 16

def incr(counter_id: str, delta: int = 1) -> None:
    shard = random.randrange(N)
    r.incrby(f"cnt:{counter_id}:{shard}", delta)

def read(counter_id: str) -> int:
    keys = [f"cnt:{counter_id}:{i}" for i in range(N)]
    return sum(int(v) for v in r.mget(keys) if v is not None)  # one round trip
```

The same shape in DynamoDB is [write sharding](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-partition-key-sharding.html): partition key `counter#<id>#<suffix>` with suffix `0..N-1`, `UpdateItem` with an `ADD` expression on write, and a read that queries all suffixes and sums (AWS's example uses suffixes 1–200 on a hot date key). A *calculated* suffix — `hash(writer_id) mod N` — keeps single-item lookups possible; a random suffix maximizes spread.

**Choosing N:** `N ≈ peak_writes_per_sec / per_shard_capacity`, with 2× headroom. For DynamoDB, per-shard capacity is that 1,000 WCU partition ceiling, so a 20k writes/s counter needs N ≥ 20, say 40. Costs of overshooting: reads touch N keys (one `mget`/`BatchGetItem`, but N units of read capacity), and cold counters carry N-key overhead — so don't shard everything, shard the keys that are actually hot, and note that resizing N up is trivial (new shards start at 0) while shrinking requires merging.

## When approximate is fine

If the product only needs "3.2M views," stop paying for exact. **Unique** counts (distinct viewers, DAU) don't shard-and-sum at all — you'd double-count — but [HyperLogLog sketches](/articles/distributed-systems/2026-08-10-hyperloglog-cardinality-estimation) merge losslessly across shards with ~0.8% error in 12 KB (`PFADD` per shard, `PFMERGE` at read). For heavy-hitter frequency counts, a count-min sketch does the same trick. The interview line: *sharding scales exact counts; sketches scale unique counts, because their merge is a union, not a sum.*

## Write-behind aggregation

The third lever is moving increments off the hot path entirely. Writers emit `+1` events to a stream (Kafka keyed by counter id, or a Redis Stream); a consumer aggregates in memory and flushes `ADD delta` to the durable row every second or so. The visible count lags by the flush interval — fine for view counters, wrong for account balances — and you must make the flush idempotent (store the consumer offset with the delta) or accept small over/under-counts on crash. This is how big view counters actually work: the "counter" your read path sees is a periodically-updated aggregate, often cached, with the true event log in the stream. It composes with sharding: aggregate per partition, sum at read.

## Choosing an approach

| Need | Reach for |
|---|---|
| Exact, real-time, high write rate | Sharded counter, read = sum |
| Unique counts (viewers, DAU) | HyperLogLog, merge at read |
| Exact eventually, massive rate | Stream + write-behind aggregation |
| Rate limiting per key | Plain `INCR` + `EXPIRE` (rarely hot enough to shard) |

**Try next:** benchmark a single Redis `INCR` key vs the 16-shard version above with `redis-benchmark` and 50 parallel clients — then check how the gap changes at N=4 and N=64 to see the read-cost/write-throughput trade directly.
