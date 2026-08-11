---
title: "Distributed Unique IDs: Snowflake vs UUIDv7 vs ULID"
date: 2026-08-11
track: distributed-systems
summary: "Auto-increment needs a coordinator and UUIDv4 wrecks B-tree insert locality, so distributed systems reach for time-ordered IDs. How Snowflake packs 64 bits, why UUIDv7 (RFC 9562, May 2024) is now the default primary key, and where ULID fits."
reading_time: 6
tags:
  - unique-ids
  - snowflake
  - uuidv7
  - ulid
  - primary-keys
  - clock-skew
sources:
  - title: "RFC 9562 — Universally Unique IDentifiers (UUIDs)"
    url: "https://www.rfc-editor.org/rfc/rfc9562.html"
  - title: "Twitter Engineering — Announcing Snowflake (2010)"
    url: "https://blog.twitter.com/engineering/en_us/a/2010/announcing-snowflake"
  - title: "twitter-archive/snowflake — original source"
    url: "https://github.com/twitter-archive/snowflake"
  - title: "ULID Specification"
    url: "https://github.com/ulid/spec"
  - title: "PostgreSQL UUID Performance: Benchmarking Random (v4) and Time-based (v7) UUIDs"
    url: "https://dev.to/umangsinha12/postgresql-uuid-performance-benchmarking-random-v4-and-time-based-v7-uuids-n9b"
---

## Why not auto-increment, and why not UUIDv4

A single `BIGINT AUTO_INCREMENT` column is the cheapest primary key in the world — until you shard. The counter lives in one place, so every insert across the fleet has to consult it, and now your ID allocator is a coordination point and a single point of failure. You can hand out ranges to reduce round trips, but you've reintroduced the exact allocation problem distributed systems try to avoid. The appeal of client-side ID generation is that any node can mint a globally unique ID with *zero* coordination.

UUIDv4 (122 random bits) gives you that: no coordinator, negligible collision risk. But randomness has a cost that shows up in your storage engine. A B-tree index keeps keys in sorted order across pages; a random key lands in an arbitrary leaf on every insert, so the working set of "hot" pages is the *entire* index. You get cache misses, random write amplification, and — because a full leaf must split to admit a key in its middle — page splits and fragmentation scattered across the tree. Insert 100M random keys and the index no longer fits the buffer pool where it matters. (LSM-tree engines tolerate random keys better than B-trees, since they buffer and sort on the way down, but even there random keys spread across more SSTables and hurt read locality.)

The fix is to make the *high bits time-ordered*. If new IDs are always slightly larger than old ones, inserts append to the right edge of the tree, keeping a small hot set and turning splits into cheap right-edge appends. That single property — a monotonic-ish prefix — is what Snowflake, UUIDv7, and ULID all buy you, in different packaging.

## Snowflake: 64 bits, hand-packed

Twitter's Snowflake (2010) was built to retire auto-increment for tweet IDs. It packs everything into a signed 64-bit integer so it fits a `BIGINT` and stays cheap to index and sort:

```
 0 | 41 bits: ms since custom epoch | 10 bits: machine id | 12 bits: sequence
 ^   ^                                ^                       ^
 |   ~69 years of milliseconds        1024 nodes             4096 ids/ms/node
 unused sign bit (keeps it positive)
```

- **41 bits of milliseconds** from a custom epoch — about 69 years of range, and the reason you pick your own epoch (start the clock in 2020, not 1970, to buy decades).
- **10 bits of machine id** — 1024 distinct workers (Twitter split it 5 datacenter + 5 worker).
- **12 bits of sequence** — a per-millisecond counter, so one node can mint 4096 IDs per millisecond before it must wait for the clock to tick.

A compact generator makes the mechanics obvious:

```python
import time, threading

EPOCH = 1_577_836_800_000  # 2020-01-01 in ms

class Snowflake:
    def __init__(self, machine_id: int):
        assert 0 <= machine_id < 1024
        self.machine_id = machine_id
        self.seq = 0
        self.last_ms = -1
        self.lock = threading.Lock()

    def next_id(self) -> int:
        with self.lock:
            now = int(time.time() * 1000)
            if now < self.last_ms:
                raise RuntimeError("clock moved backwards; refusing to mint")
            if now == self.last_ms:
                self.seq = (self.seq + 1) & 0xFFF   # 12-bit wrap
                if self.seq == 0:                   # exhausted this ms
                    while now <= self.last_ms:
                        now = int(time.time() * 1000)
            else:
                self.seq = 0
            self.last_ms = now
            return ((now - EPOCH) << 22) | (self.machine_id << 10) | self.seq
```

Two hazards are baked into that code. The `machine_id` must be globally unique — reuse one and two nodes will emit identical IDs in the same millisecond. Teams assign it via ZooKeeper/etcd leases, a config service, or the pod's ordinal in a StatefulSet; never a hard-coded constant. And Snowflake trusts the wall clock. If NTP steps time backwards, IDs would go non-monotonic or collide, so the generator *refuses to mint* until the clock catches up. This is the same physical-clock fragility that [hybrid logical clocks](/distributed-systems/2026-07-26-hybrid-logical-clocks) exist to tame — Snowflake just fails loudly instead.

## UUIDv7: the standardized default

For years the ecosystem improvised time-ordered UUIDs from an IETF draft. RFC 9562 (published **May 2024**, obsoleting RFC 4122) made it official, adding versions 6, 7, and 8. UUIDv7 is the one that matters for primary keys. Its 128 bits are:

```
| 48 bits: Unix ms timestamp | 4: ver(0111) | 12: rand_a | 2: variant | 62: rand_b |
```

The most significant 48 bits are a plain Unix-epoch millisecond timestamp — no custom epoch, no bit-shifting — followed by version/variant markers and 74 bits of randomness (optionally a sub-millisecond counter for monotonicity). Because the timestamp sits in the high bits, UUIDv7 sorts by creation time and inserts near the right edge of a B-tree, while keeping UUIDv4's coordination-free, collision-resistant generation. Postgres 18 shipped a native `uuidv7()` function in 2026, and benchmarks on time-ordered vs random UUIDs consistently show lower insert-time index bloat and faster range scans. If you want one recommendation for a new schema, it's this: store it as a native 16-byte `uuid`/`UUID` type, not a 36-char string.

## ULID: sortable and human-friendly

ULID predates the RFC and solves the same problem with a different emphasis: a nice text form. It's 128 bits — a **48-bit millisecond timestamp** plus **80 bits of randomness** — encoded in Crockford base32 (no `I`, `L`, `O`, `U`) as a **26-character string** that is lexicographically sortable as text. Sort ULID strings and you get time order for free, which is handy in logs, URLs, and object keys. The spec also defines a monotonic mode: within the same millisecond, increment the random field instead of redrawing it, so IDs stay strictly increasing under bursty generation.

## Choosing between them

| Property | Auto-increment | UUIDv4 | Snowflake | UUIDv7 | ULID |
|---|---|---|---|---|---|
| Size | 64-bit | 128-bit | 64-bit | 128-bit | 128-bit |
| Coordination | central counter | none | machine-id assignment | none | none |
| Sortable / k-sorted | strict | no | k-sorted (ms) | k-sorted (ms) | k-sorted (ms) |
| DB index locality | excellent | poor | good | good | good |
| Collision risk | none (serialized) | negligible | none if ids unique | negligible | negligible |
| Embedded timestamp | no | no | yes (ms) | yes (ms) | yes (ms) |
| Nice text form | yes | ok | numeric | ok | best (26 chars) |

"k-sorted" is the honest term: these IDs are ordered *to the millisecond*, not strictly. Two IDs minted in the same millisecond on different nodes can interleave either way — fine for index locality, not a substitute for a logical order. If you need cross-node causal ordering, that's a job for logical clocks, and if you're sharding on these keys, note that a time prefix concentrates all *current* writes on whichever shard owns "now," an anti-pattern for [consistent hashing](/distributed-systems/2026-07-25-consistent-hashing-ring)-style placement.

One caveat worth saying out loud in an interview: every time-ordered ID **leaks its creation timestamp**. Anyone holding a UUIDv7 or ULID can read roughly when the row was created and estimate creation rates by diffing sequential IDs — a real concern for user-facing identifiers. When that matters, use a random UUIDv4 for the external handle and keep the sortable ID internal.

The practical default in 2026: UUIDv7 for database primary keys, ULID when you want the same ordering with a compact readable string, and Snowflake when 64 bits and a `BIGINT` column are worth the machine-id plumbing.

**Try next:** generate 1M UUIDv4 keys and 1M UUIDv7 keys into two Postgres tables, then compare index size with `pg_relation_size` and buffer-cache hit ratio during inserts — watch the random keys balloon the index and thrash the cache while the v7 keys stay lean.
