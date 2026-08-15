---
title: "Distributed Unique IDs: Snowflake vs UUIDv7 vs ULID"
date: 2026-08-11
track: distributed-systems
summary: "Auto-increment needs a coordinator and UUIDv4 wrecks B-tree insert locality, so distributed systems reach for time-ordered IDs. How Snowflake packs 64 bits, how UUIDv7 (RFC 9562, May 2024) is specified, and where ULID fits."
reading_time: 7
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

**Gist.** A sharded system cannot use a single auto-increment counter without turning the allocator into a coordination point, and the coordination-free alternative — 122 random bits, UUIDv4 — destroys insert locality in a B-tree index. The shared mechanism of Snowflake, UUIDv7 and ULID is to place **a millisecond timestamp in the most significant bits** and fill the remainder with node identity, a sequence counter, or randomness, so new keys land at the right edge of the index. The cost is that ordering is only **k-sorted** — accurate to the millisecond, not a total order — and that every such identifier **discloses its creation time**.

## Why not auto-increment, and why not UUIDv4

A single `BIGINT AUTO_INCREMENT` column is the cheapest primary key available until the table is sharded. The counter lives in one place, so every insert across the fleet consults it: the allocator becomes both a coordination point and a single point of failure. Handing out ranges reduces round trips but restores the allocation problem in a coarser form. Client-side generation removes it entirely — any node mints a globally unique identifier with **zero coordination**.

UUIDv4 provides exactly that with 122 random bits and negligible collision risk, but randomness has a cost inside the storage engine. A B-tree index keeps keys sorted across pages; a random key lands in an arbitrary leaf on every insert, so the set of hot pages is **the entire index**. The consequences are cache misses, random write amplification, and page splits: a full leaf that must admit a key in its middle splits, leaving fragmentation scattered across the tree rather than concentrated at one edge. Past a large enough index the working set no longer fits the buffer pool. Log-structured merge-tree (LSM-tree) engines tolerate random keys better, because they buffer and sort on the way down, but random keys still spread across more sorted string tables (SSTables) and degrade read locality.

Making the high bits time-ordered restores the invariant that matters: **if each new identifier is at least as large as its predecessors, inserts append near the right edge**, the hot set stays small, and splits become cheap right-edge appends. A monotonic-ish prefix is the single property Snowflake, UUIDv7 and ULID all supply, in different packaging.

## Snowflake: 64 bits, hand-packed

Twitter's Snowflake (2010) replaced auto-increment for tweet identifiers. It packs everything into a signed 64-bit integer, so the value fits a `BIGINT` column and stays cheap to index and sort:

```
 0 | 41 bits: ms since custom epoch | 10 bits: machine id | 12 bits: sequence
 ^   ^                                ^                       ^
 |   ~69 years of milliseconds        1024 nodes             4096 ids/ms/node
 unused sign bit (keeps it positive)
```

- **41 bits of milliseconds** from a custom epoch — approximately 69 years of range. The epoch is chosen per deployment, so a system starting its clock at 2020 rather than 1970 gains the intervening decades of range.
- **10 bits of machine identifier** — 1024 distinct workers. Twitter split the field into 5 datacenter bits and 5 worker bits.
- **12 bits of sequence** — a per-millisecond counter, bounding a single node at **4096 identifiers per millisecond**; beyond that the generator must wait for the clock to tick.

Two invariants carry the design. First, **the machine identifier must be globally unique at any instant**: two nodes sharing one identifier emit identical values whenever their sequence counters coincide within the same millisecond, and the collision is silent — the duplicate surfaces later as a primary-key violation or an overwritten row. Assignment is therefore delegated to a lease in ZooKeeper or etcd, a configuration service, or the ordinal of a pod in a StatefulSet, rather than a compiled-in constant.

Second, **Snowflake trusts the wall clock**. A backwards step from the Network Time Protocol (NTP) would produce identifiers below ones already issued, breaking monotonicity and potentially reissuing a (timestamp, sequence) pair. The generator's state machine handles this by comparing the current millisecond against the last one observed: equal means increment the sequence and, on 12-bit wraparound, spin until the clock advances; greater means reset the sequence to zero; **less means refuse to mint**. Availability is traded for uniqueness. This is the physical-clock fragility that [hybrid logical clocks](/distributed-systems/2026-07-26-hybrid-logical-clocks) address; Snowflake instead fails loudly.

### Implementation sketch (Scala)

```scala
final class Snowflake(machineId: Long, epochMs: Long):
  require(machineId >= 0 && machineId < 1024)

  private var lastMs: Long = -1L
  private var seq: Long = 0L

  def nextId(): Long = synchronized:
    var now = System.currentTimeMillis()
    if now < lastMs then
      throw new IllegalStateException("clock moved backwards; refusing to mint")
    if now == lastMs then
      seq = (seq + 1) & 0xFFFL          // 12-bit sequence, wraps to 0
      if seq == 0 then                  // millisecond exhausted: spin to the next tick
        while now <= lastMs do now = System.currentTimeMillis()
    else seq = 0L
    lastMs = now
    ((now - epochMs) << 22) | (machineId << 10) | seq
```

The shift widths encode the layout: the sequence occupies bits 0–11, the machine identifier bits 12–21, the timestamp everything above. `synchronized` is load-bearing — the read-modify-write over `lastMs` and `seq` must be atomic, or two threads observing the same millisecond can leave with the same sequence value.

## UUIDv7: the standardized layout

Time-ordered UUIDs circulated as an IETF draft for years before **RFC 9562 (May 2024)**, which obsoletes RFC 4122 and adds versions 6, 7 and 8. Version 7 is the one RFC 9562 recommends for new applications that need a time-ordered identifier. Its 128 bits are laid out as:

```
| 48 bits: Unix ms timestamp | 4: ver(0111) | 12: rand_a | 2: variant | 62: rand_b |
```

The most significant 48 bits hold a plain Unix-epoch millisecond timestamp — no custom epoch and no shifting — followed by the version and variant markers and 74 bits that RFC 9562 permits to be random, or to carry a sub-millisecond counter when strictly increasing values within a millisecond are required. Because the timestamp occupies the high bits, UUIDv7 sorts by creation time and inserts near the right edge of a B-tree while retaining UUIDv4's coordination-free, collision-resistant generation. PostgreSQL 18 provides a native `uuidv7()` function. The cited PostgreSQL benchmark compares random and time-based UUIDs and reports an advantage for the time-based form. Storage representation matters independently of the version: a native 16-byte `uuid` column is less than half the size of the 36-character text rendering, and index size follows key size.

## ULID: sortable and human-readable

ULID predates the RFC and targets the same ordering property with an emphasis on the text form. It is 128 bits — a **48-bit millisecond timestamp** followed by **80 bits of randomness** — encoded in Crockford base32, which omits `I`, `L`, `O` and `U`, producing a **26-character string that is lexicographically sortable as text**. Sorting the strings therefore yields time order without decoding, which suits log lines, URLs and object-store keys. The specification also defines a monotonic mode: within a single millisecond the random field is incremented rather than redrawn, so consecutive identifiers from one generator strictly increase under bursty load.

## Choosing between them

| Property | Auto-increment | UUIDv4 | Snowflake | UUIDv7 | ULID |
|---|---|---|---|---|---|
| Size | 64-bit | 128-bit | 64-bit | 128-bit | 128-bit |
| Coordination | central counter | none | machine-id assignment | none | none |
| Sortable / k-sorted | strict | no | k-sorted (ms) | k-sorted (ms) | k-sorted (ms) |
| DB index locality | excellent | poor | good | good | good |
| Collision risk | none (serialized) | negligible | none if ids unique | negligible | negligible |
| Embedded timestamp | no | no | yes (ms) | yes (ms) | yes (ms) |
| Text form | yes | ok | numeric | ok | 26 chars |

"k-sorted" is the accurate term: these identifiers are ordered **to the millisecond, not strictly**. Two identifiers minted in the same millisecond on different nodes may interleave in either direction. That is sufficient for index locality and insufficient as a substitute for a logical order; cross-node causal ordering requires logical clocks. Sharding on such a key has a further consequence: a time prefix concentrates all current writes on whichever shard owns the present millisecond, which conflicts with the uniform spread [consistent hashing](/distributed-systems/2026-07-25-consistent-hashing-ring) is chosen for.

Every time-ordered identifier also **discloses its creation timestamp**. A holder of a UUIDv7 or ULID can read approximately when the row was created, and differences between identifiers issued in sequence expose creation rates. Where that inference is unacceptable, a random UUIDv4 can serve as the externally visible handle while the sortable identifier stays internal.

**Try next:** insert 1M UUIDv4 keys and 1M UUIDv7 keys into two PostgreSQL tables, then compare index size with `pg_relation_size` and the buffer-cache hit ratio observed during the inserts.

## Pitfalls

- **A duplicated Snowflake machine identifier does not fail fast.** Two nodes configured with the same value collide only when their sequence counters coincide inside one millisecond; the symptom is a sporadic primary-key violation or a silently overwritten row, hours after the misconfiguration.
- **An NTP step backwards halts minting.** The generator refuses to issue identifiers below `lastMs`, so the symptom is a stalled write path rather than corrupt data — an availability failure caused by trusting the wall clock.
- **Sequence exhaustion becomes a spin loop.** More than 4096 requests within one millisecond on one node drives the generator into busy-waiting for the next tick; the symptom is latency pinned to the millisecond boundary under burst load.
- **Storing a UUID as a 36-character string inflates the index.** The key is 36 bytes instead of 16, so the index holds fewer entries per page and the ordering advantage of v7 is partly spent on width.
- **UUIDv7 is only k-sorted, so same-millisecond order is arbitrary.** Code that treats identifier order as event order will reorder concurrent events across nodes.
- **A time-prefixed shard key hot-spots on "now".** All current writes route to one shard because the leading bits of every fresh identifier are near-identical.
- **Non-monotonic ULID generation breaks intra-millisecond ordering.** Without the specification's monotonic mode, the 80 random bits are redrawn per call, so two identifiers from the same millisecond can sort in either order.
