---
title: "Leaderboards and Top-K: Sorted Sets, Sharding, and Sketches"
date: 2026-08-15
track: sys-patterns
summary: "One Redis sorted set answers score updates, rank queries, and top-10 pages in O(log N) — until cardinality, write volume, or an unbounded key space breaks it. This is the escalation ladder: single ZSET, time-windowed keys with TTLs, sharded scatter-gather merge, and finally sketch-plus-heap serving when you can't afford to count everything exactly."
reading_time: 5
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

"Design a game leaderboard" and "find the top 10 most-played songs" look like the same question. They aren't: one has a **bounded, known member set** (players with scores) and wants *exact ranks*; the other has an **unbounded key space** (every song, URL, hashtag) and wants *approximate heavy hitters*. The design ladder below climbs from one to the other.

## Rung 1: one sorted set

A Redis **sorted set** is a dual structure — hash map (member → score) plus skip list (score-ordered; [how skip lists work](/articles/distributed-systems/2026-08-14-skip-lists)) — so every leaderboard operation is logarithmic or better: `ZADD`/`ZINCRBY` are **O(log N)** per member, `ZRANK` is **O(log N)**, `ZRANGE` is **O(log N + M)** for an M-row page, `ZSCORE` is **O(1)**.

```bash
ZINCRBY lb:global 120 player:9042        # award points
ZREVRANGE lb:global 0 9 WITHSCORES       # top-10 page
ZREVRANK lb:global player:9042           # my rank (0-based, descending)
ZREVRANGE lb:global 4995 5004 WITHSCORES # page around rank ~5000
```

Back-of-envelope: a ZSET entry costs roughly 100–200 bytes with overhead, so 10M players ≈ 1–2 GB — one node holds it, and log₂(10⁷) ≈ 23 comparison steps per update means single-node Redis sustains hundreds of thousands of updates/sec. **Most leaderboards never need rung 2.** Say that in the interview before scaling anything.

**Tie-breaking:** equal scores order lexicographically by member, which is arbitrary and unstable as IDs go. The standard fix packs "earlier wins" into the score: `score = points * 2^20 + (2^20 - 1 - seconds_since_epoch_bucket)` — points in the high bits, inverted timestamp in the low bits. Mind the ceiling: Redis scores are doubles, exact only for integers up to **2⁵³**, so budget your bit split.

## Rung 2: windowed leaderboards

Daily/weekly boards are *separate keys per window*, written in the same update:

```lua
-- KEYS[1]=lb:global KEYS[2]=lb:daily:{date} ARGV[1]=delta ARGV[2]=member ARGV[3]=ttl
redis.call('ZINCRBY', KEYS[1], ARGV[1], ARGV[2])
redis.call('ZINCRBY', KEYS[2], ARGV[1], ARGV[2])
redis.call('EXPIRE',  KEYS[2], ARGV[3])          -- e.g. 2 * 86400
return redis.call('ZREVRANK', KEYS[1], ARGV[2])
```

The Lua script makes the multi-key update atomic and returns the new rank in one round trip. Key per window (`lb:daily:2026-08-15`, `lb:weekly:2026-W33`) with a TTL of ~2× the window means expiry is your archival policy — yesterday's board stays queryable for a grace period, then vanishes. "Last 24 hours" *rolling* windows don't fit ZSETs directly; approximate with hourly keys merged via `ZUNIONSTORE`, or accept fixed windows (players do).

## Rung 3: sharding for huge cardinality

When one key can't hold the set — 500M members, or write volume past one node — shard the ZSET by hash of member across P shards. Each query changes character:

- **Top-K page:** scatter-gather — fetch top K from every shard, merge K·P candidates in a heap, keep K. Cheap for K=100, P=16.
- **My exact rank:** `rank = Σ over shards of (members with score > mine)` — P × `ZCOUNT` (O(log N) each). Fine at read time for a profile page; too hot for every render, so cache it.
- **Serving layer trick:** don't scatter-gather on user traffic. A refresher merges shard tops into a small `lb:top100` ZSET every second or two; reads hit that. Ranks outside the top page can be *approximate* — keep a score histogram (fixed buckets, sharded counters) and report "rank ≈ 1.2M, top 3%." Nobody at rank 1,203,411 audits the digit. The hot-write side of this — one member's counter melting a shard — is [the sharded-counter problem](/articles/sys-patterns/2026-08-13-sharded-counters-hot-keys).

## Rung 4: top-K over unbounded streams

"Top 10 trending hashtags" can't ZADD every hashtag ever seen. The classic answer is a **count-min sketch for frequencies plus a small min-heap of current leaders** — the sketch itself is covered in [the count-min article](/articles/distributed-systems/2026-08-14-count-min-sketch-heavy-hitters); the *serving design* is what's new here. Each stream worker maintains sketch+heap over its partition; periodically (say every 10 s) it flushes heap contents into a shared serving ZSET, which the API reads like any rung-1 board. The sketch bounds memory to KBs regardless of cardinality; the ZSET makes serving boring. **HeavyKeeper** (USENIX ATC 2018) improves on CMS+heap by probabilistically decaying small flows — it's the algorithm behind Redis's built-in `TOPK.ADD`/`TOPK.LIST`, which collapses this whole rung into one command family if you're on Redis Stack.

| Design | Exactness | Memory | Fits when |
|---|---|---|---|
| Single ZSET | Exact ranks | O(N), ~150 B/member | ≤ tens of millions of members |
| Windowed keys + TTL | Exact per window | O(N) per live window | daily/weekly boards |
| Sharded + merged top page | Exact top-K, approx deep ranks | O(N) across shards | 100M+ members, hot writes |
| Sketch + heap / TOPK | Approximate (no underestimates from CMS) | O(1)-ish, KBs | unbounded key space, trends |

**Try next:** load 1M fake players into one ZSET, then implement the rung-3 scatter-gather over 4 shards and diff its top-100 and rank answers against the single-key truth — then throttle the refresher to 10 s and watch what staleness does to "my rank" right after a score update.
