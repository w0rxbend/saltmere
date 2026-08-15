---
title: "Redis, Memcached and Valkey: A Structured Comparison"
date: 2026-08-10
track: microservices
summary: "A comparison of Memcached and Redis across data model, threading, memory management, persistence and clustering, with the version facts on I/O threads and Valkey's multi-threading stated precisely, and a decision table."
reading_time: 8
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

**Gist.** Memcached and Redis both keep hot data in memory in front of a slower store, but they expose different units of mutation: Memcached exposes an opaque byte string per key, Redis exposes typed structures that can be mutated server-side by a single command. The structural difference forces a different concurrency story — a flat blob store makes every partial update a read-modify-write on the client, which is not atomic across concurrent writers — and it is paid for in surface area: Redis adds persistence, replication, eviction policies and a much larger command set, each of which is a configuration and failure mode that Memcached does not have.

## Data model: opaque value versus typed structure

Memcached's model is flat. A key maps to an opaque byte string, capped **by default at 1 MB per item**. The command surface is essentially `set`, `get`, `add`, `incr`, `delete`. Updating one field of a cached object requires fetching the blob, deserializing, mutating, reserializing and writing it back.

Redis exposes native structures. Per the Redis documentation the core types are **strings, hashes, lists, sets, sorted sets and streams**, plus strings used as bitmaps and bitfields, HyperLogLog for cardinality estimation, and geospatial indexes. Newer builds and modules add JSON, probabilistic types (Bloom filter, count-min sketch, Top-K, t-digest) and vector sets. The consequence for application code is that a value can be mutated **in place, server-side, in one command**, without transferring the whole value.

```
# A leaderboard, maintained server-side.
ZADD game:scores 4200 "alice" 3990 "bob" 5100 "carol"
ZINCRBY game:scores 150 "bob"          # single command, no read-modify-write
ZREVRANGE game:scores 0 2 WITHSCORES   # top 3, already ordered

# Update one field of a cached user without rewriting the rest.
HSET user:1001 name "Ada" plan "pro" seats 12
HINCRBY user:1001 seats 1
HGET user:1001 plan
```

The same leaderboard on a flat blob store requires the application to hold a serialized ordered list and rewrite the entire blob on every score change. **Two writers that read the same blob version and write back independently produce a lost update: the second write overwrites the first writer's increment.** The invariant "the stored blob reflects every increment issued" is not maintainable by the store, because the store sees only whole-value writes and has no way to reject one that was computed from a stale read.

### Implementation sketch (Scala)

The sketch below contrasts the two mutation paths. It is deliberately abstract: the point is where the atomicity boundary sits, not any client library's API.

```scala
// The flat model: whole values only.
trait BlobCache:
  def get(key: String): Option[Array[Byte]]
  def set(key: String, value: Array[Byte]): Unit

// The structure-server model: the field is the unit of mutation.
trait FieldCache:
  def hincrBy(key: String, field: String, delta: Long): Long

final case class User(name: String, seats: Int)

// Read-modify-write. The window between get and set is unguarded:
// a concurrent caller that also read `before` overwrites this result.
def addSeatBlob(c: BlobCache, key: String, delta: Int,
                decode: Array[Byte] => User, encode: User => Array[Byte]): Unit =
  c.get(key) match
    case Some(before) =>
      val u = decode(before)
      c.set(key, encode(u.copy(seats = u.seats + delta)))  // lost-update window
    case None => ()

// One command; the server serialises concurrent increments on the same field
// because command execution runs on a single thread.
def addSeatField(c: FieldCache, key: String, delta: Int): Long =
  c.hincrBy(key, "seats", delta.toLong)
```

The second form removes the window by moving the read and the write into the same server-side command. It does not remove the need for correct invalidation, and it constrains the value to a shape the server understands.

## Threading

**Memcached is multi-threaded.** Command processing is spread across a thread pool, so simple get/set traffic can occupy several cores in one process.

**Redis is, in the wording of its own documentation, "mostly, a single threaded server from the POV of command execution".** Command execution runs on one thread driven by an event loop over `epoll`/`kqueue`. That single execution thread is what makes each command atomic with respect to every other command without locking on the data path. The qualifier matters:

- Since **Redis 6**, I/O threads (`io-threads`) can move socket writes — and, when reads are also offloaded, socket reads and **command parsing** — off the main thread. **Command execution remains on the main thread.**
- **Redis 8** (general availability May 2025, with AGPLv3 added as a licence option) shipped a reworked I/O threading implementation. Redis reports **up to approximately 112% more throughput** from that work, with gains varying by command mix and hardware. **The default remains `io-threads 1`.**

**Valkey**, the BSD-licensed fork, extends the same split. **Valkey 8.0** (September 2024) keeps single-threaded command execution and offloads command reading and parsing, response writing and I/O event polling to dedicated I/O worker threads. The project reports throughput rising **from 360K to approximately 1.19M requests per second with 8 I/O threads and 512-byte values**, roughly a 230% increase.

The accurate summary: **Redis and Valkey parallelize I/O, not command execution; Memcached parallelizes command processing itself.** Valkey caching specifics are covered in [its own article](/articles/microservices/2026-07-27-valkey-8-caching).

## Memory management

Memcached uses a **slab allocator**. Memory is carved into fixed-size chunk classes and an item is placed in the smallest class that fits it. Allocation within a class is therefore a free-list pop rather than a search, but **space is wasted whenever an item is materially smaller than its chunk class, and memory already assigned to one slab class is not readily reclaimed for another** — the condition known as slab calcification, which appears as evictions in one size class while another holds free chunks. Eviction is **per-slab-class LRU** (least recently used).

Redis and Valkey use **jemalloc** by default and expose a set of eviction policies applied when `maxmemory` is reached: `noeviction` (writes are rejected), `allkeys-lru`, `allkeys-lfu`, `allkeys-random`, `volatile-lru`, `volatile-lfu`, `volatile-random` and `volatile-ttl`. The `volatile-*` variants consider only keys carrying a time-to-live (TTL); the `allkeys-*` variants consider every key. **LFU (least frequently used) admits a key on repeated access rather than a single access, so a one-pass scan over cold keys displaces less of the hot set than it does under LRU.**

## Persistence

Memcached has **no persistence**: after a process restart the cache is empty. The nuance is **extstore**, the flash-storage feature, which keeps keys and small values in RAM and places larger values on SSD, extending capacity beyond DRAM. **It is a capacity extension, not a durability guarantee.**

Redis offers two mechanisms. **RDB** writes point-in-time snapshots: compact and fast to load, with **everything written since the last snapshot lost on an unclean stop**. **AOF** (append-only file) logs every write and replays it at startup: more durable, with configurable `fsync` policy, and larger files. Both can be enabled together. This is the basis for using Redis as a lightweight primary store or a durable queue, and it is why a requirement that the cache survive a restart selects Redis over Memcached.

## Clustering and replication

Memcached has no built-in replication or cluster protocol. Sharding is client-side, typically consistent hashing over a server pool, and the server provides no failover.

Redis and Valkey ship asynchronous primary-to-replica replication, Redis Sentinel for automated failover, and Redis Cluster, which partitions the keyspace across **16,384 hash slots** with client redirection to the owning node. Valkey 8 improved cluster scaling and replication, including dual-channel synchronisation. High availability and horizontal scale provided by the datastore rather than the client therefore weigh toward Redis or Valkey.

## Decision table

| Dimension | Memcached | Redis / Valkey |
|---|---|---|
| Data model | Flat key → blob (≤1 MB default) | Strings, hashes, lists, sets, sorted sets, streams, bitmaps, HyperLogLog, geospatial, more |
| Threading | Multi-threaded command processing | Single-threaded execution; I/O threads (Redis 6+/8, Valkey 8) parallelize I/O |
| Memory | Slab allocator, per-slab-class LRU | jemalloc, 8 eviction policies (allkeys/volatile × lru/lfu/random/ttl) |
| Persistence | None (extstore extends capacity, not durability) | RDB snapshots, AOF log |
| Replication / HA | Client-side sharding only | Replication, Sentinel, Redis Cluster (16,384 slots) |
| Pub/sub, streams, Lua, transactions | No | Yes |
| Atomic server-side operations | Limited (`incr`/`decr`) | Per-structure commands, `MULTI`/`EXEC`, scripts |

Memcached fits a workload that is a volatile cache of opaque values — rendered fragments, serialized sessions, computed responses — where multi-threaded get/set is the whole requirement, the data need not survive a restart, and sharding is acceptable on the client. Redis or Valkey fit where the cache also carries counters, ordered sets, rate-limiting state, event streams, per-field updates, TTL-aware eviction policy, persistence or built-in failover.

The claim "Redis is single-threaded, therefore Memcached is faster" does not follow. The single execution thread is what provides per-command atomicity; I/O threading in Redis 8 and Valkey 8 raises throughput by the figures cited above; and for any value that is not a flat blob, Redis performs in one command what a blob store can only approximate with a read-modify-write that admits lost updates.

## Pitfalls

- **Enabling `io-threads` and expecting execution parallelism.** Throughput on a mixed workload stays flat because I/O threads offload socket reads, writes and parsing only; execution remains on the main thread.
- **Quoting the ~112% Redis 8 and ~230% Valkey 8 figures as general speedups.** Both come from vendor benchmarks with a specific thread count, value size and command mix (Valkey's at 8 I/O threads and 512-byte values); a different value-size distribution moves the ratio.
- **Treating extstore as durability.** Values on SSD do not make the cache survive as a source of truth; extstore extends capacity past DRAM and nothing more.
- **Assuming a Memcached item can exceed 1 MB.** Writes above the default item limit fail rather than being split, so large serialized objects are silently absent from the cache and every request falls through to the origin.
- **Choosing `volatile-lru` on a keyspace where most keys have no TTL.** Only keys with a TTL are eligible for eviction, so once those are exhausted the instance behaves like `noeviction` and writes are rejected at `maxmemory`.
- **Relying on client-side read-modify-write for counters.** Two concurrent updaters that read the same value both write back a value derived from it, and one increment disappears with no error reported.
- **Adding a Redis replica and treating it as a consistency boundary.** Replication is asynchronous, so a read served by a replica can miss a write already acknowledged by the primary.
- **Sizing Memcached from total item bytes.** The slab allocator rounds each item up to a chunk class, so usable capacity is below the configured limit by an amount that depends on the item-size distribution.
