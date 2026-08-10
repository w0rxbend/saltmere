---
title: "Redis vs Memcached (and Valkey): The Interview Comparison, Done Properly"
date: 2026-08-10
track: microservices
summary: "A precise, no-hand-waving comparison of Memcached and Redis across data model, threading, memory, persistence, and clustering — with the version facts on I/O threads and Valkey's multi-threading right, and a decision table you can defend in an interview."
reading_time: 7
tags: [redis, memcached, valkey, caching, microservices]
sources:
  - title: "Redis Data Types (redis.io)"
    url: "https://redis.io/docs/latest/develop/data-types/"
  - title: "Redis 8 GA: Fast, scalable, and feature-rich (redis.io)"
    url: "https://redis.io/blog/redis-8-ga/"
  - title: "Unlock 1 Million RPS: Triple the Speed with Valkey (valkey.io)"
    url: "https://valkey.io/blog/unlock-one-million-rps/"
  - title: "Flash Storage / extstore (Memcached Documentation)"
    url: "https://docs.memcached.org/features/flashstorage/"
  - title: "Redis benchmarks & single-threaded model (redis.io)"
    url: "https://redis.io/docs/latest/operate/oss_and_stack/management/optimization/benchmarks/"
---

"Redis or Memcached?" is one of those interview questions that sounds like a preference and is really a test of whether you understand what a cache *is*. The honest answer is that they solve overlapping problems from opposite starting points: Memcached is a lean, multi-threaded, volatile key→blob store that does one thing extremely well; Redis is a data-structure server that happens to be excellent at caching. Get the specifics right — especially the threading and versioning facts, which are where candidates most often overstate things — and the "it depends" becomes a defensible decision.

## Data model: a blob versus a toolbox

Memcached's model is deliberately flat. A key maps to an opaque byte string, capped by default at 1 MB per item. You `set`, `get`, `add`, `incr`, `delete`. That is essentially the whole API. If you want to update one field of a cached object, you fetch the blob, deserialize it, mutate it, reserialize, and write it back — a full round trip.

Redis exposes native structures, and this is the difference that actually changes application code. Per the Redis docs, the core types are strings, hashes, lists, sets, sorted sets, and streams, plus strings-as-bitmaps and bitfields, HyperLogLog for cardinality estimation, and geospatial indexes. Newer builds and modules add JSON, probabilistic types (Bloom, Count-min sketch, Top-K, t-digest), and vector sets. The practical upshot: you manipulate data *in place*, server-side, atomically, without shipping the whole value over the wire.

Two quick examples of what that buys you:

```
# A leaderboard — impossible to do cheaply with a flat blob store.
ZADD game:scores 4200 "alice" 3990 "bob" 5100 "carol"
ZINCRBY game:scores 150 "bob"          # atomic bump, no read-modify-write
ZREVRANGE game:scores 0 2 WITHSCORES   # top 3, already sorted

# Update one field of a cached user without touching the rest.
HSET user:1001 name "Ada" plan "pro" seats 12
HINCRBY user:1001 seats 1               # atomic, server-side
HGET user:1001 plan
```

With Memcached, the leaderboard would mean maintaining a serialized sorted list in your app and rewriting the entire blob on every score change — with a race condition every time two writers overlap.

## Threading: get the facts exactly right

This is where interviews separate the careful from the glib.

**Memcached is multi-threaded by design.** It scales command processing across cores with a thread pool, so on a big box it can saturate many cores for simple get/set traffic without extra process management.

**Redis is "mostly single-threaded from the point of view of command execution."** That phrasing is from Redis's own docs, and the nuance matters. Command execution runs on one thread against an epoll/kqueue event loop, which is what gives Redis its atomicity guarantees and predictable latency — no locks on the data path. But "single-threaded" is not the whole story:

- Since **Redis 6**, I/O threads (`io-threads`) can offload socket reads/writes and command *parsing* off the main thread. Command *execution* still happens on the main thread.
- **Redis 8** (GA May 1, 2025, now offered under AGPLv3) shipped a reworked I/O threading implementation. Redis reports up to ~112% throughput improvement with `io-threads 8` on a multi-core Intel CPU, though gains vary by command mix. The default is still `io-threads 1`.

**Valkey** — the BSD-licensed fork — pushed this further. **Valkey 8.0** (September 2024) keeps single-threaded command execution but offloads reading/parsing commands, writing responses, polling I/O events, and even freeing memory to dedicated I/O worker threads, with thread affinity so the same client tends to hit the same I/O thread. The project's benchmark: throughput from 360K to ~1.19M RPS (8 I/O threads, 512-byte values), roughly a 230% jump. The correct interview summary is: *Redis and Valkey parallelize I/O, not command execution; Memcached parallelizes command processing itself.* Valkey caching specifics live in [its own article](/articles/microservices/2026-07-27-valkey-8-caching) — I won't duplicate them here.

## Memory management

Memcached uses a **slab allocator**: memory is carved into fixed-size chunk classes, and items land in the smallest class that fits. This makes allocation fast and fragmentation-resistant, but it can waste space when item sizes don't match a class (slab calcification), and eviction is per-slab-class **LRU**.

Redis (and Valkey) use **jemalloc** by default and give you a menu of eviction policies rather than one LRU. When `maxmemory` is hit you choose the behavior: `noeviction` (reject writes), `allkeys-lru`, `allkeys-lfu`, `allkeys-random`, `volatile-lru`, `volatile-lfu`, `volatile-random`, and `volatile-ttl`. The `volatile-*` variants only evict keys that carry a TTL; `allkeys-*` consider everything. LFU (least-frequently-used) is often the better default for a cache because it resists one-off scans polluting the hot set. The eviction-policy trade-offs are a deep-dive of their own elsewhere in this series; here it's enough to know Redis gives you the dial and Memcached largely doesn't.

## Persistence

Memcached has **no persistence** — restart the process and the cache is empty. That is a feature for a pure cache: nothing to corrupt, nothing to warm-restore, no I/O tax. The one nuance is **extstore / flash storage**, which lets Memcached keep keys and small values in RAM while spilling larger values to SSD, extending capacity beyond DRAM. It is a capacity extension, not a durability guarantee.

Redis offers two durability mechanisms. **RDB** takes point-in-time snapshots (compact, fast to load, but you can lose everything since the last snapshot). **AOF** logs every write and replays it on restart (more durable, configurable fsync, larger files). They can run together. This is why Redis can double as a lightweight primary store or a durable queue — and why "we need the cache to survive a restart" is a legitimate reason to pick Redis over Memcached.

## Clustering and replication

Memcached has no built-in replication or cluster protocol; sharding is client-side (consistent hashing across a pool). Simple, but the server gives you no failover.

Redis and Valkey ship replication (async primary→replica), Redis Sentinel for automated failover, and Redis Cluster — a hash-slot design (16,384 slots) that shards data across nodes with client redirection. Valkey 8 improved cluster scaling and replication (dual-channel sync). So if you need HA and horizontal scale from the datastore itself rather than the client, that weighs toward Redis/Valkey.

## Decision table

| Dimension | Memcached | Redis / Valkey |
|---|---|---|
| Data model | Flat key → blob (≤1 MB default) | Strings, hashes, lists, sets, sorted sets, streams, bitmaps, HLL, geo, more |
| Threading | Multi-threaded command processing | Single-threaded execution; I/O threads (Redis 6+/8, Valkey 8) parallelize I/O |
| Memory | Slab allocator + per-slab LRU | jemalloc + 8 eviction policies (allkeys/volatile × lru/lfu/random/ttl) |
| Persistence | None (extstore = SSD capacity, not durability) | RDB snapshots + AOF log |
| Replication / HA | Client-side sharding only | Replication, Sentinel, Redis Cluster (16,384 slots) |
| Pub/sub, streams, Lua, transactions | No | Yes |
| Atomic server-side ops | Limited (incr/decr) | Rich (per-structure, MULTI/EXEC, scripts) |
| Best at | Huge volume of simple, ephemeral values | Rich data, durability, messaging, atomicity |

## So which one?

Reach for **Memcached** when the workload is genuinely a pure, volatile cache of large-ish opaque values — rendered HTML fragments, serialized session blobs, computed API responses — where you want the simplest possible multi-threaded get/set, don't need the data to survive a restart, and shard from the client. Its narrowness is the point: fewer knobs, fewer failure modes.

Reach for **Redis or Valkey** when you need *more than a cache*: leaderboards and counters (sorted sets, atomic incr), rate limiters (bitmaps, sorted sets), pub/sub or streams for events, per-field updates (hashes), TTL-aware LFU eviction, or persistence and built-in HA. In practice most microservice teams land here because the cache inevitably grows a second job — and if licensing matters, Valkey gives you the same engine under BSD.

The trap answer is "Redis is single-threaded so Memcached is faster." It isn't that simple: Redis's single execution thread buys atomicity, its I/O threads (and Valkey's multi-threaded I/O) close much of the throughput gap, and for anything beyond flat blobs Redis does in one atomic command what Memcached needs a lossy read-modify-write to fake.

**Try next:** benchmark both under *your* value-size distribution with `memtier_benchmark`, enable `io-threads` on a Redis 8 box and re-measure, then port one hot read path to a Redis hash and delete the read-modify-write code it replaces.
