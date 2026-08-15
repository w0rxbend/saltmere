---
title: "Distributed ID Generation: Snowflake, UUIDv7, and the Bit Math"
date: 2026-08-10
track: distributed-systems
summary: "The design of a unique-ID generator broken down by requirement — unique, roughly time-sortable, high-throughput, no single point of failure — and the approaches that trade off against each other: UUIDv4 vs UUIDv7 (RFC 9562), ULID/KSUID, database ticket servers, and Twitter Snowflake's 41/10/12 bit layout, with a generator sketch, the clock-skew hazards, and the effect of time-sortable keys on B-tree inserts."
reading_time: 7
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

**Gist.** A system that must mint identifiers on many nodes at once cannot ask a central counter for each one without making that counter both a bottleneck and a single point of failure. The prevailing designs remove per-identifier coordination by **coordinating once** — each generator receives a distinct identity — and then packing a timestamp in the high bits, that identity in the middle, and a per-tick counter in the low bits, so that uniqueness and approximate time ordering fall out of the layout. The cost is that correctness now depends on the wall clock: **if the clock moves backwards, a node can re-mint a triple it has already issued**.

## The requirements, and why they conflict

Four requirements are usually stated together:

1. **Unique** — no two identifiers collide, across all generators.
2. **Roughly time-sortable** — newer identifiers sort after older ones, so a range scan over the key is a time window and the primary key stays append-mostly.
3. **High throughput** — no network round-trip per identifier.
4. **No single point of failure** — no central sequence server whose outage halts all writes.

No single design maximizes all four. A central counter yields dense, perfectly ordered identifiers and is a single point of failure. Pure randomness needs no coordination at all and, at 122 random bits, is collision-free in practice, but carries no ordering. The interesting designs sit between the two, and the space between them is largely a question of how a 64- or 128-bit integer is sliced into a timestamp, a machine identity, and a per-tick counter.

## The naive baselines

**Database auto-increment.** A `BIGSERIAL` column on one Postgres instance is dense and sortable. It becomes a bottleneck and a single point of failure as soon as the data is sharded, because every insert contends on one sequence.

**Ticket servers (the Flickr approach).** Two MySQL instances, one issuing odd numbers and one even, via `REPLACE INTO` against a single-row table. This removes the single point of failure and keeps ordering roughly monotonic, but throughput is bounded by what those two databases sustain, and adding a third server requires re-partitioning the number space. The technique buys time rather than scale.

The common lesson: **per-identifier coordination is the constraint**. The designs that scale coordinate exactly once, to assign each node a distinct identity, and thereafter generate locally with no further exchange.

## UUIDv4 and UUIDv7

A universally unique identifier (UUID) is 128 bits. **UUIDv4**, specified in **RFC 9562**, is 122 bits of randomness plus a 4-bit version and a 2-bit variant. Generation is embarrassingly parallel and requires no coordination, satisfying requirements 1, 3 and 4. It fails requirement 2 outright: consecutive identifiers land at unrelated positions in the key space.

That randomness has a cost at the storage layer. Inserting random keys into a B-tree index directs each insert to an arbitrary leaf page, producing page splits, cache misses and write amplification; the working set at the tail of the index is never small. Time-sortable keys instead direct successive inserts to the **same rightmost page**, so the tree grows append-mostly and splits are rarer. This is the index-locality argument developed in the companion piece on [LSM-trees vs B-trees](/articles/distributed-systems/2026-08-10-lsm-trees-vs-b-trees).

**UUIDv7**, also standardized in RFC 9562, places the timestamp first:

- **48 bits** — `unix_ts_ms`, big-endian Unix time in milliseconds (most significant)
- **4 bits** — version (`0b0111`)
- **12 bits** — `rand_a`, random
- **2 bits** — variant (`0b10`)
- **62 bits** — `rand_b`, random

Because the millisecond timestamp occupies the top bits, **two identifiers minted in different milliseconds sort byte-wise in time order**; identifiers minted within the same millisecond are ordered by their random bits, so RFC 9562 describes the ordering as monotonic only at millisecond granularity unless one of its optional counter methods is used. UUIDv7 therefore retains UUIDv4's zero-coordination property while adding time locality, in a 128-bit value that existing UUID columns already store.

**ULID** and **KSUID** predate UUIDv7 and use different string encodings. A **ULID** is 128 bits — a 48-bit millisecond timestamp plus 80 bits of randomness — rendered as 26 Crockford base32 characters, with a monotonic mode that **increments the random field** for identifiers generated in the same millisecond so that they remain ordered. A **KSUID** is 20 bytes — a 32-bit *second*-granularity timestamp with a custom epoch of 2014-05-13 plus 128 random bits — rendered as 27 base62 characters. All three share one structural idea: timestamp on the left, entropy on the right.

## Twitter Snowflake

Where a compact **64-bit integer** is required — half the storage of a UUID, and a fit for a native `bigint` column — Snowflake is the canonical layout. It packs a signed 64-bit integer as **1 + 41 + 10 + 12**:

| Bits | Field | Meaning |
|-----:|-------|---------|
| 1 | sign | unused, held at 0 so the integer stays positive |
| 41 | timestamp | milliseconds since a **custom epoch** (Twitter used `1288834974657`, i.e. 2010-11-04) |
| 10 | machine id | which node minted the identifier — **1,024** distinct workers |
| 12 | sequence | per-millisecond counter — **4,096** identifiers per millisecond per node |

The arithmetic is the substance of the design. 2^41 milliseconds is approximately **69 years** measured from the custom epoch rather than from 1970. Ten machine bits admit 1,024 nodes. Twelve sequence bits allow each node 4,096 identifiers **per millisecond**, on the order of **4 million per second per node**, after which the generator must wait for the clock to advance. Coordination occurs exactly once: some external mechanism — ZooKeeper, etcd, static configuration, or a StatefulSet pod ordinal — assigns each node a unique 10-bit identity. Thereafter every node generates offline.

The invariant that makes the scheme correct is that the triple **(timestamp, machine id, sequence)** is never repeated. The machine id is unique by assignment; the sequence is unique within a millisecond by the counter; the timestamp is assumed non-decreasing.

### Implementation sketch (Scala)

```scala
final class Snowflake(machineId: Long):
  require(machineId >= 0 && machineId <= Snowflake.MaxMachine)

  private var lastTs: Long = -1L
  private var seq: Long    = 0L

  def nextId(): Long = synchronized:
    var ts = System.currentTimeMillis()
    if ts < lastTs then
      throw IllegalStateException(s"clock moved back ${lastTs - ts} ms")
    if ts == lastTs then
      seq = (seq + 1) & Snowflake.MaxSequence
      if seq == 0 then ts = waitNextMs()   // 4096 exhausted in this millisecond
    else seq = 0L
    lastTs = ts
    ((ts - Snowflake.Epoch) << (Snowflake.MachineBits + Snowflake.SequenceBits)) |
      (machineId << Snowflake.SequenceBits) | seq

  private def waitNextMs(): Long =
    var ts = System.currentTimeMillis()
    while ts <= lastTs do ts = System.currentTimeMillis()
    ts

object Snowflake:
  val Epoch: Long        = 1288834974657L
  val MachineBits: Int   = 10
  val SequenceBits: Int  = 12
  val MaxMachine: Long   = (1L << MachineBits) - 1    // 1023
  val MaxSequence: Long  = (1L << SequenceBits) - 1   // 4095
```

The masking `& MaxSequence` is the overflow detector: the counter wrapping to zero is the signal that the millisecond's 4,096 slots are spent and the generator must block until the clock ticks.

## The clock moving backwards

Snowflake's uniqueness rests on the assumption that **the wall clock never moves backward**. Network Time Protocol (NTP) corrections, leap-second smearing and virtual-machine live migration can each move it back. When that happens, a node can re-issue a `(timestamp, machine, sequence)` triple it has already used, producing a **duplicate primary key** with no error at the point of generation. The sketch above refuses to issue identifiers while `ts < lastTs`, which converts silent duplication into a visible failure; it does not prevent the skew. Persisting `lastTs` extends the protection across process restarts. The usual mitigation is to run NTP in **slew** mode rather than **step** mode, so that a correction is spread over many small adjustments to the clock rate instead of a single jump that can land in the past. The difficulty of establishing a common time is treated separately in [clock synchronization: Cristian & NTP](/articles/distributed-systems/2026-07-30-clock-synchronization-cristian-ntp).

**Sonyflake** re-slices the same 63 bits: **39 bits of time in 10 ms units** (roughly 174 years), **8 sequence bits** and **16 machine bits** — 65,536 nodes, but 256 identifiers per 10 ms per node. It exchanges single-node burst rate for node count and horizon. **Instagram** folded the shard into the identifier instead: a PL/pgSQL function builds a 64-bit value from **41 bits of millisecond timestamp, 13 bits of logical shard id, and 10 bits of per-shard sequence** (modulo 1024). The shard bits make the identifier itself indicate which database holds the row, merging identifier generation with [data partitioning](/articles/distributed-systems/2026-08-10-data-partitioning-sharding). The same 64-bit budget, spent three ways.

## Choosing

| Approach | Bits | Sortable | Coordination | Applicable when |
|----------|-----:|----------|--------------|-----------------|
| DB auto-increment | 64 | yes (dense) | central, single point of failure | single node, low scale |
| Ticket server (Flickr) | 64 | roughly | 2+ databases | mid-scale, integer keys required |
| UUIDv4 | 128 | **no** | none | scattered keys acceptable |
| UUIDv7 / ULID / KSUID | 128/160 | yes | none | general case |
| Snowflake | 64 | yes | once (node id) | compact integers, high burst |
| Sonyflake | 64 | yes | once (node id) | many nodes, long horizon |

The through-line: decentralize by giving each generator a distinct identity, put the timestamp in the high bits so that ordering follows from the encoding, and treat the clock as the adversary.

## Pitfalls

- **Two nodes assigned the same machine id mint colliding identifiers indefinitely.** The collisions surface as duplicate-key errors at insert time, far from the misconfigured node; the cause is a machine id derived from something non-unique, such as a hash of a hostname that repeats after a redeploy.
- **A backwards clock step re-issues an already-used triple.** The generator has no record of which timestamps it has served unless `lastTs` is persisted, so a process restart after a backwards step reopens a window that was already consumed.
- **A node sustaining more than 4,096 identifiers per millisecond spins in the wait loop.** The symptom is a busy-wait consuming a core with latency pinned to the millisecond boundary; the cause is burst rate exceeding the sequence field, not a lock.
- **UUIDv4 primary keys degrade B-tree insert throughput as the index outgrows memory.** Each insert targets a random leaf page, so the buffer pool cannot hold the working set and every insert becomes a read; the effect is absent while the index still fits in memory, which is why it appears only after growth.
- **Timestamps embedded in identifiers are readable by anyone holding one.** UUIDv7, ULID, KSUID and Snowflake all expose creation time in the high bits, and Instagram-style layouts additionally expose the shard.
- **A custom epoch is a permanent commitment.** Changing it re-maps every future identifier into a range that may overlap already-issued values, and identifiers stored earlier can no longer be decoded with the new epoch.
