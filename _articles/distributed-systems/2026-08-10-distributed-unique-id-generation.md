---
title: "Designing a Distributed ID Generator: Snowflake, UUIDv7, and the Bit Math"
date: 2026-08-10
track: distributed-systems
summary: "The classic design-a-unique-ID-generator interview question broken down by requirement — unique, roughly time-sortable, high-throughput, no single point of failure — and the approaches that trade off against each other: UUIDv4 vs the modern UUIDv7 (RFC 9562), ULID/KSUID, database ticket servers, and Twitter Snowflake's 41/10/12 bit layout, with a 40-line generator, the clock-skew hazards, and why time-sortable keys keep B-tree inserts cheap."
reading_time: 6
tags:
  - unique-ids
  - snowflake
  - uuidv7
  - distributed-systems
  - system-design
sources:
  - title: "RFC 9562: Universally Unique IDentifiers (UUIDs) — UUIDv7 & UUIDv4 (IETF, 2024)"
    url: "https://www.rfc-editor.org/rfc/rfc9562.html"
  - title: "bwmarrin/snowflake — Twitter Snowflake bit layout & epoch (README)"
    url: "https://github.com/bwmarrin/snowflake/blob/master/README.md"
  - title: "ULID Specification (ulid/spec)"
    url: "https://github.com/ulid/spec"
  - title: "Sharding & IDs at Instagram (Instagram Engineering)"
    url: "https://instagram-engineering.com/sharding-ids-at-instagram-1cf5a71e5a5c"
  - title: "sony/sonyflake — 39/8/16 bit variant, and segmentio/ksuid"
    url: "https://github.com/sony/sonyflake/blob/master/README.md"
---

## The question behind the question

"Design a system that hands out unique IDs at scale" is a system-design staple because it forces you to name your requirements precisely and then watch them fight each other. Write them down first — this is half the interview:

1. **Unique** — no two IDs ever collide, across all generators, forever.
2. **Roughly time-sortable** — newer IDs sort after older ones, so a range scan is a time window and the primary key stays append-mostly.
3. **High-throughput** — tens of thousands of IDs per second per node, with no network round-trip per ID.
4. **No single point of failure** — no central sequence server whose outage stops all writes.

You cannot maximize all four with one design. A central counter gives you dense, perfectly ordered IDs but is a SPOF. Pure randomness is trivially decentralized and collision-free but destroys ordering. Everything interesting lives in between, and the "in between" is mostly about how you slice a 64- or 128-bit integer into a timestamp, a machine identity, and a per-tick counter.

## The naive baselines and why they fall over

**Database auto-increment.** `BIGSERIAL` in one Postgres box is dense, sortable, and dead simple — and it's a bottleneck and a SPOF the moment you shard. Every insert contends on one sequence.

**Ticket servers (the Flickr approach).** Run two MySQL boxes, one handing out odd numbers, one even, via `REPLACE INTO` on a single-row table. This removes the single SPOF and ordering stays roughly monotonic, but throughput is capped at what a couple of databases can do, and adding a third server means re-partitioning the number space. It buys time, not scale.

The lesson: **coordination per ID is the enemy.** Good designs coordinate *once* — assign each node a distinct identity — then let every node mint IDs locally with zero chatter.

## UUIDs: v4 vs the v7 you should actually use

A UUID is 128 bits. **UUIDv4** (RFC 9562) is 122 bits of randomness plus a 4-bit version and 2-bit variant. Collisions are astronomically unlikely, generation is embarrassingly parallel, and there's no coordination at all — it nails requirements 1, 3, and 4. It fails requirement 2 completely: consecutive IDs land at random positions in the key space.

That randomness has a real cost at the storage layer. Insert random keys into a B-tree index and every insert hits a random leaf page, causing page splits, cache misses, and write amplification — the index never stays hot at the tail. Time-sortable keys, by contrast, keep inserting into the *same* rightmost page, so the tree grows append-mostly and splits are rare. (This is exactly the index-locality argument in the companion piece on [LSM-trees vs B-trees](/articles/distributed-systems/2026-08-10-lsm-trees-vs-b-trees).)

**UUIDv7**, standardized in **RFC 9562 (2024)**, fixes this and is the modern default. Its layout:

- **48 bits** — `unix_ts_ms`, big-endian Unix time in milliseconds (the most-significant bits)
- **4 bits** — version (`0b0111`)
- **12 bits** — `rand_a`, random
- **2 bits** — variant (`0b10`)
- **62 bits** — `rand_b`, random

Because the millisecond timestamp sits in the top bits, byte-wise lexicographic order equals time order. You get UUIDv4's zero-coordination decentralization *and* time-locality, in a drop-in 128-bit value your database already knows how to store. If someone asks "UUID or Snowflake?", the honest 2026 answer usually starts with "UUIDv7, unless you specifically need 64 bits."

**ULID** and **KSUID** predate UUIDv7 with nicer string encodings. A **ULID** is 128 bits — 48-bit ms timestamp + 80-bit randomness — as 26 Crockford base32 characters, with a monotonic mode that *increments the random field* for same-millisecond IDs so they stay ordered. A **KSUID** is 20 bytes — a 32-bit *second*-granularity timestamp (custom epoch 2014-05-13) plus 128 random bits — as 27 base62 characters. All three share one idea: timestamp on the left, entropy on the right.

## Twitter Snowflake: 64 bits, no coordinator per ID

When you need a compact **64-bit integer** (half the storage of a UUID, and it fits a native `bigint` column and CPU register), Snowflake is the canonical answer. Twitter's original layout packs a signed 64-bit int as **1 + 41 + 10 + 12**:

| Bits | Field | Meaning |
|-----:|-------|---------|
| 1 | sign | unused, kept 0 so the int stays positive |
| 41 | timestamp | ms since a **custom epoch** (Twitter used `1288834974657`, i.e. 2010-11-04) |
| 10 | machine id | which node minted this — **1,024** distinct workers |
| 12 | sequence | per-millisecond counter — **4,096** IDs/ms/node |

The bit math is the whole point, so make it explicit in the interview. 41 bits of milliseconds is 2^41 ms ≈ **69 years** from the custom epoch (a custom epoch buys you those years instead of burning them on time since 1970). 10 machine bits means 1,024 nodes; 12 sequence bits means each node can mint 4,096 IDs *per millisecond* — roughly **4 million IDs/sec/node** — before it must spin-wait for the clock to tick. Coordination happens exactly once: something (ZooKeeper, etcd, config, or the pod ordinal) assigns each node a unique 10-bit id. After that, every node generates locally, offline, forever. That's requirement 4 satisfied without a per-ID round-trip.

Here's the generator — the bit-packing is the load-bearing part:

```python
import time, threading

class Snowflake:
    EPOCH = 1288834974657          # custom epoch (ms)
    MACHINE_BITS  = 10
    SEQUENCE_BITS = 12
    MAX_MACHINE  = (1 << MACHINE_BITS)  - 1   # 1023
    MAX_SEQUENCE = (1 << SEQUENCE_BITS) - 1   # 4095

    def __init__(self, machine_id: int):
        if not 0 <= machine_id <= self.MAX_MACHINE:
            raise ValueError("machine_id out of range")
        self.machine_id = machine_id
        self.seq = 0
        self.last_ts = -1
        self.lock = threading.Lock()

    def _now(self) -> int:
        return int(time.time() * 1000)

    def next_id(self) -> int:
        with self.lock:
            ts = self._now()
            if ts < self.last_ts:                 # clock went backwards
                raise RuntimeError(f"clock moved back {self.last_ts - ts}ms")
            if ts == self.last_ts:                 # same ms: bump sequence
                self.seq = (self.seq + 1) & self.MAX_SEQUENCE
                if self.seq == 0:                  # 4096 exhausted this ms
                    ts = self._wait_next_ms(ts)
            else:
                self.seq = 0
            self.last_ts = ts
            return (((ts - self.EPOCH) << (self.MACHINE_BITS + self.SEQUENCE_BITS))
                    | (self.machine_id << self.SEQUENCE_BITS)
                    | self.seq)

    def _wait_next_ms(self, ts: int) -> int:
        while ts <= self.last_ts:
            ts = self._now()
        return ts
```

## The hazard everyone under-tests: the clock going backwards

Snowflake's uniqueness rests on one assumption — **the wall clock never moves backward**. But NTP corrections, leap-second smearing, and VM live-migration all let `time()` jump back. If it does, a node can re-mint a `(timestamp, machine, sequence)` triple it already used, silently producing a **duplicate primary key**. The snippet above refuses to issue IDs while `ts < last_ts` — the standard defensive move. Production systems go further: reject for small skews, or better, persist `last_ts` and only serve time from a monotonic source. Twitter's guidance was blunt — run NTP in *slew* mode, never *step* mode, so the clock is nudged rather than yanked. (For why "one true time" is so hard, see [clock synchronization: Cristian & NTP](/articles/distributed-systems/2026-07-30-clock-synchronization-cristian-ntp).)

**Sonyflake** (Sony's variant) re-slices the same 63 bits to change these trade-offs: **39 bits of time in 10ms units** (~174 years of life), **8 sequence bits**, and **16 machine bits** — so 65,536 nodes but only 256 IDs per 10ms per node. It trades single-node burst rate for far more machines and a longer horizon. **Instagram** took a different tack entirely, folding the *shard* into the ID: their PL/pgSQL function builds a 64-bit id from **41 bits ms timestamp + 13 bits logical shard id + 10 bits per-shard sequence** (mod 1024). The shard bits mean the ID itself tells you which database holds the row — ID generation and [data partitioning](/articles/distributed-systems/2026-08-10-data-partitioning-sharding) become the same decision. Same 64-bit budget, three different ways to spend it.

## Choosing under pressure

| Approach | Bits | Sortable? | Coordination | Best when |
|----------|-----:|-----------|--------------|-----------|
| DB auto-increment | 64 | yes (dense) | central SPOF | single node / low scale |
| Ticket server (Flickr) | 64 | roughly | 2+ DBs | mid-scale, want ints |
| UUIDv4 | 128 | **no** | none | scattered keys OK |
| UUIDv7 / ULID / KSUID | 128/160 | yes | none | **modern default** |
| Snowflake | 64 | yes | once (node id) | compact ints, high burst |
| Sonyflake | 64 | yes | once (node id) | many nodes, long horizon |

The through-line: decentralize by giving each generator a distinct identity, put the timestamp in the high bits so ordering falls out for free, and treat the clock as the adversary. If you can afford 128 bits, reach for UUIDv7; if you must have 64, reach for Snowflake and guard the clock.

**Try next:** implement the monotonic-within-a-millisecond guarantee for UUIDv7 (increment `rand_a` on same-ms collisions, per RFC 9562 Method 1) and measure B-tree insert throughput for UUIDv4 vs UUIDv7 keys on a table of 10M rows.
