---
title: "Leaderboards and Top-K: Sorted Sets, Sharding, and Sketches"
date: 2026-08-15
track: sys-patterns
summary: "One Redis sorted set answers score updates, rank queries, and top-10 pages in O(log N) — until cardinality, write volume, or an unbounded key space breaks it. The escalation ladder runs from a single sorted set through time-windowed keys with expiry, sharded scatter-gather merge, and finally sketch-plus-heap serving where exact counting is not affordable."
reading_time: 6
tags: [leaderboard, top-k, redis, sorted-sets, sharding, probabilistic-data-structures]
sources:
  - title: "Redis docs — Leaderboard use case"
    url: "https://redis.io/docs/latest/develop/use-cases/leaderboard/"
  - title: "Redis docs — Sorted sets data type"
    url: "https://redis.io/docs/latest/develop/data-types/sorted-sets/"
  - title: "Redis docs — ZADD command (complexity)"
    url: "https://redis.io/docs/latest/commands/zadd/"
  - title: "Gong et al. — HeavyKeeper: An Accurate Algorithm for Finding Top-k Elephant Flows (USENIX ATC 2018)"
    url: "https://www.usenix.org/conference/atc18/presentation/gong"
  - title: "Redis docs — Top-K probabilistic data type"
    url: "https://redis.io/docs/latest/develop/data-types/probabilistic/top-k/"
---

**Gist.** "Design a game leaderboard" and "find the top 10 most-played songs" appear to be one problem but differ in the input: the first has a **bounded, enumerable member set** and requires *exact ranks*, the second an **unbounded key space** (every song, URL, hashtag) and tolerates *approximate heavy hitters*. A Redis sorted set answers the first exactly in O(log N) per operation; a frequency sketch paired with a small heap answers the second in bounded memory. The cost of the ladder between them is progressively weaker answers: sharding makes deep ranks a scatter-gather aggregate rather than a single lookup, and sketching abandons exactness altogether.

## Rung 1: one sorted set

A Redis **sorted set** (ZSET) is a dual structure — a hash map from member to score, plus a skip list ordered by score ([how skip lists work](/articles/distributed-systems/2026-08-14-skip-lists)). The hash map serves point lookups, the skip list serves order. Every leaderboard operation is therefore logarithmic or better: `ZADD` and `ZINCRBY` are **O(log N)** per member, `ZRANK` is **O(log N)**, `ZRANGE` is **O(log N + M)** for an M-row page, and `ZSCORE` is **O(1)**.

```bash
ZINCRBY lb:global 120 player:9042        # award points
ZREVRANGE lb:global 0 9 WITHSCORES       # top-10 page
ZREVRANK lb:global player:9042           # rank, 0-based, descending
ZREVRANGE lb:global 4995 5004 WITHSCORES # page around rank ~5000
```

Memory is O(N): every entry stores the member string, its score, and the skip list's forward pointers, so the structure grows with membership and not with update volume. The work per update grows only logarithmically — log₂(10⁷) ≈ 23 levels to traverse for a ten-million-member set — so the limit that ends rung 1 is memory on one node or write throughput on one node, not the cost of an individual operation. **Rung 1 is sufficient for a leaderboard whose member set and write rate fit a single instance**, and the later rungs are responses to specific limits rather than a default architecture.

**Tie-breaking.** Members with equal scores are ordered lexicographically by member, which is arbitrary and changes if identifier formats change. The conventional fix encodes the tiebreak into the score itself — points in the high bits, an inverted timestamp in the low bits, so that an earlier submission sorts ahead of a later one at equal points:

    score = points * 2^20 + (2^20 - 1 - seconds_since_epoch_bucket)

The ceiling is arithmetic, not conventional: Redis scores are IEEE 754 double-precision floats, which represent integers exactly only up to **2⁵³**. The bit split between points and timestamp must fit under that bound, or increments silently stop changing the score.

## Rung 2: windowed leaderboards

Daily and weekly boards are *separate keys per window*, written in the same update as the all-time board:

```lua
-- KEYS[1]=lb:global KEYS[2]=lb:daily:{date} ARGV[1]=delta ARGV[2]=member ARGV[3]=ttl
redis.call('ZINCRBY', KEYS[1], ARGV[1], ARGV[2])
redis.call('ZINCRBY', KEYS[2], ARGV[1], ARGV[2])
redis.call('EXPIRE',  KEYS[2], ARGV[3])          -- e.g. 2 * 86400
return redis.call('ZREVRANK', KEYS[1], ARGV[2])
```

The Lua script executes as one unit, so the two increments are applied together, and the new rank returns in the same round trip rather than a second query. With one key per window (`lb:daily:2026-08-15`, `lb:weekly:2026-W33`) and a time-to-live of roughly twice the window length, **expiry is the archival policy**: the previous window remains queryable through a grace period and then disappears without a cleanup job. A *rolling* "last 24 hours" window does not map onto this scheme, because ZSET scores carry no per-increment timestamp to age out. The approximations available are hourly keys combined with `ZUNIONSTORE` at read time, or fixed windows accepted as the product behaviour.

## Rung 3: sharding for high cardinality

When one key cannot hold the member set, or write volume exceeds one node, the ZSET is split across P shards by hash of the member. Every query changes character under this split:

- **Top-K page.** Scatter-gather: fetch the local top K from each of the P shards and merge the K·P candidates through a heap, keeping K. Correctness rests on an invariant of hash sharding — **each member lives on exactly one shard**, so a global top-K member is necessarily in its own shard's local top K.
- **Exact rank of one member.** `rank = Σ over shards of (members with a score above this one)`, that is P executions of `ZCOUNT`, each O(log N). Acceptable for a profile page rendered occasionally; too expensive to run on every render, so the result is cached.
- **Serving layer.** User traffic does not scatter-gather. A background refresher merges the shard tops into a small `lb:top100` ZSET on a fixed interval, and reads hit that single key. Ranks outside the visible top page can be reported *approximately* from a score histogram of fixed buckets with sharded counters — "rank ≈ 1.2M, top 3%" — which avoids the per-request fan-out entirely. The write-side hazard of this design, a single member's counter saturating one shard, is [the sharded-counter problem](/articles/sys-patterns/2026-08-13-sharded-counters-hot-keys).

### Implementation sketch (Scala)

The merge step of rung 3: P per-shard top-K lists arrive already sorted descending, and a bounded min-heap keeps the global K without materialising K·P in sorted order.

```scala
final case class Entry(member: String, score: Double)

/** Merge per-shard top-K lists into the global top K.
  * Valid only under hash sharding, where each member appears on one shard. */
def mergeTopK(shardTops: Seq[Seq[Entry]], k: Int): Vector[Entry] =
  // Min-heap on score: the weakest survivor sits at the head and is evicted first.
  val heap = scala.collection.mutable.PriorityQueue.empty[Entry](
    Ordering.by[Entry, Double](_.score).reverse
  )
  for
    shard <- shardTops
    entry <- shard
  do
    if heap.size < k then heap.enqueue(entry)
    else if entry.score > heap.head.score then
      heap.dequeue()
      heap.enqueue(entry)
  heap.dequeueAll.reverse.toVector   // dequeueAll yields ascending; reverse for a page

/** Exact rank by aggregation: count strictly better scores on every shard.
  * countAbove(shard, score) is one ZCOUNT against that shard. */
def exactRank(shards: Seq[Int], score: Double)(
    countAbove: (Int, Double) => Long): Long =
  shards.map(countAbove(_, score)).sum
```

## Rung 4: top-K over unbounded streams

"Top 10 trending hashtags" cannot admit a `ZADD` for every hashtag ever observed, because the member set has no bound. The established answer is a **count-min sketch for frequency estimation plus a small min-heap of current leaders**; the sketch itself is covered in [the count-min article](/articles/distributed-systems/2026-08-10-count-min-sketch), and the serving arrangement is what differs here. Each stream worker maintains a sketch and heap over its own partition and periodically flushes the heap contents into a shared serving ZSET, which the application programming interface reads exactly like a rung-1 board. **The sketch bounds memory independently of cardinality; the ZSET keeps the read path unchanged.** The error direction matters for interpretation: a count-min sketch never underestimates a frequency, since hash collisions only add counts, so a reported leader may be inflated but a true leader is never undercounted below its real value.

**HeavyKeeper** (Gong et al., USENIX ATC 2018) refines the sketch-plus-heap arrangement: when a different key hashes onto an occupied counter, the counter is decayed with a probability that falls as the stored count grows, so small flows are displaced from a cell while large ones persist in it. It is the algorithm underlying the Redis `TOPK.ADD` and `TOPK.LIST` commands in the probabilistic data type family.

| Design | Exactness | Memory | Fits when |
|---|---|---|---|
| Single ZSET | Exact ranks | O(N) | member set and write rate fit one node |
| Windowed keys + expiry | Exact per window | O(N) per live window | daily/weekly boards |
| Sharded + merged top page | Exact top-K, approximate deep ranks | O(N) across shards | member set or write rate exceeds one node |
| Sketch + heap / TOPK | Approximate, no underestimates from the sketch | kilobytes | unbounded key space, trends |

## Pitfalls

- **Score bits overflow the double.** Packing points and an inverted timestamp into one score past 2⁵³ makes increments round to no change; the symptom is a player whose score stops moving while events keep arriving.
- **Lexicographic tie-breaks reorder on identifier change.** Equal-score members sort by member string, so migrating from numeric to UUID identifiers silently permutes an entire tier of the board.
- **Non-atomic multi-key updates diverge.** Incrementing the global and windowed keys as separate commands leaves the windowed board short by one event when the connection drops between them; the Lua script exists to remove that window.
- **`ZUNIONSTORE` over many hourly keys is not free.** Its cost grows with the total number of elements in the inputs, so a rolling window built from 24 hourly keys does substantially more work than a page read from one key.
- **Scatter-gather rank on the request path.** P `ZCOUNT` calls per profile render multiplies read load by the shard count and makes tail latency the slowest shard's latency, not the median's.
- **Refresher staleness is visible immediately after a score update.** With a serving ZSET rebuilt on an interval, a player whose score has been recorded sees the pre-update rank until the next merge, which reads as a lost submission.
- **Sketch overestimates promote near-misses.** Because a count-min sketch never underestimates, an item ranked immediately below the true top-K can be reported inside it when its counters collide with a heavy hitter's.
