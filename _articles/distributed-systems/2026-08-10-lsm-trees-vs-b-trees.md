---
title: "LSM-Trees vs B-Trees: The Two Storage Engines Under Every Database"
date: 2026-08-10
track: distributed-systems
summary: Every OLTP database picks one of two on-disk shapes. B-trees update pages in place, keep everything sorted for cheap reads and range scans, and lean on a write-ahead log for crash safety — the engine under PostgreSQL and InnoDB. LSM-trees buffer writes in a memtable, flush immutable sorted SSTables, and merge them in the background — the engine under RocksDB, Cassandra, and LevelDB. This walks the mechanics of both, the three amplifications (write / read / space) each one trades, a concrete LSM write→flush→compaction and read path, and how to pick.
reading_time: 6
tags:
  - lsm-tree
  - b-tree
  - storage-engines
  - databases
  - compaction
sources:
  - title: "Designing Data-Intensive Applications, Ch.3 (Kleppmann) — Storage and Retrieval"
    url: "https://dataintensive.net/"
  - title: "The Log-Structured Merge-Tree (O'Neil, Cheng, Gawlick, O'Neil, Acta Informatica 1996)"
    url: "https://www.cs.umb.edu/~poneil/lsmtree.pdf"
  - title: "Compaction — facebook/rocksdb Wiki"
    url: "https://github.com/facebook/rocksdb/wiki/Compaction"
  - title: "Leveled Compaction — facebook/rocksdb Wiki"
    url: "https://github.com/facebook/rocksdb/wiki/Leveled-Compaction"
  - title: "Read, write & space amplification — B-Tree vs LSM (Mark Callaghan, Small Datum)"
    url: "http://smalldatum.blogspot.com/2015/11/read-write-space-amplification-b-tree.html"
---

"How does your database store a row on disk?" is really an invitation to pick a side in a decades-old argument. Two answers are in production use, and both solve the same problem: durably persist sorted key-value pairs on a device that is slow at random I/O. The **B-tree** keeps data sorted in fixed-size pages and mutates them in place. The **LSM-tree** never mutates anything — it appends, then merges. Understanding *why* those philosophies diverge, and what each one costs, is the whole interview.

## The B-tree: update in place

A B-tree stores keys in fixed-size **pages** (typically 4 KB), arranged as a balanced tree. One page is the root; interior pages hold key ranges and pointers to child pages; leaf pages hold the actual values. To read a key, you start at the root and follow pointers down — `O(log n)` page reads, and because the tree is shallow (a 4-level tree of 4 KB pages indexes hundreds of GB), that is a handful of seeks.

A write finds the leaf page for the key and **overwrites it in place**. If the page is full, it splits in two and the parent is updated to point at both. This in-place mutation is the defining trait, and it is dangerous: a split touches multiple pages, and a crash mid-split leaves a corrupt tree with a dangling pointer. B-trees defend against this with a **write-ahead log (WAL)** — an append-only file where every modification is recorded *before* the pages are touched, so a restart can replay it to restore consistency. (This is the same write-ahead discipline behind [durable outbox and write-behind patterns](/articles/distributed-systems/2026-08-10-cache-database-consistency-dual-write) elsewhere in the corpus.) PostgreSQL and MySQL's InnoDB are both B-tree engines.

The payoff: reads are excellent and *predictable*. Every key lives in exactly one place, so a point lookup is a fixed, small number of page reads, and a **range scan** is a sequential walk across sorted leaves. The cost: writes do random I/O. Updating one 200-byte row can dirty a whole 4 KB page (and the WAL entry), and a page split can rewrite several — the source of a B-tree's **write amplification**.

## The LSM-tree: buffer, flush, merge

The LSM-tree, introduced by O'Neil et al. in 1996, refuses to do random writes at all. It has three moving parts:

1. **Memtable** — an in-memory sorted structure (a red-black tree or skip list). Every write goes here first. Because it is RAM, inserts are fast and stay sorted.
2. **WAL** — before a write touches the memtable, it is appended to an on-disk log, so a crash cannot lose the not-yet-flushed memtable.
3. **SSTables** (Sorted String Tables) — when the memtable reaches a size threshold, it is flushed to disk as an **immutable, sorted** file. Once written, an SSTable is never modified.

An update to an existing key does *not* find and rewrite the old value — it just writes a new entry to the memtable. A delete writes a **tombstone**. Over time you accumulate many SSTables, each sorted internally but overlapping in key ranges, with newer files shadowing older ones. Left alone, this would make reads slow and waste space on dead versions. The fix is **compaction**: background threads merge SSTables, keep the newest value for each key, drop shadowed values and tombstones, and write fresh consolidated SSTables. RocksDB, Cassandra, LevelDB, and ScyllaDB all run this loop.

### Leveled vs size-tiered compaction

How you organize SSTables is the single biggest tuning knob, and it maps directly onto the amplification trade.

- **Size-tiered / universal**: SSTables of similar size are merged together into a bigger one; each level holds several overlapping runs. Per-level write amplification is near 1, so it is cheap to write — but a key can live in many runs, so reads and space suffer (dead data lingers, and a huge merge transiently needs room for both input and output).
- **Leveled** (LevelDB/RocksDB default): each level `L` is capped at roughly 10× the size of `L-1`, and within a level SSTables are non-overlapping — one sorted run per level. A key exists in at most one file per level. This minimizes space and read amplification, at the cost of higher write amplification, since promoting a file into level `L` rewrites overlapping files there. The RocksDB wiki states the trade plainly: leveled "minimizes space amplification at the cost of read and write amplification"; tiered "minimizes write amplification at the cost of read and space amplification." (RocksDB actually uses a hybrid — L0 is tiered, deeper levels leveled.)

### Bloom filters make reads bearable

A read might have to consult every SSTable that could hold the key. To avoid touching files that don't, each SSTable carries a **Bloom filter** — a probabilistic set-membership structure. Query the filter first; if it says "definitely not here," skip the whole file with no disk read. Bloom filters have no false negatives (they never hide a key that exists) but do have false positives, so a rare wasted read still happens. This is exactly the membership-guard role covered in [Bloom vs cuckoo filters](/articles/distributed-systems/2026-08-10-cuckoo-filters-vs-bloom) — cuckoo filters add deletion support, which matters when SSTables come and go under compaction.

## The three amplifications

Every engine trades three quantities (Mark Callaghan's framing on *Small Datum* is the standard reference):

- **Write amplification** — bytes written to disk per byte of logical write. B-trees pay it via page rewrites + WAL; LSM leveled compaction pays it re-merging levels.
- **Read amplification** — I/Os per logical read. B-tree: ~tree height. LSM: memtable + potentially several SSTables (mitigated by Bloom filters and leveling).
- **Space amplification** — bytes on disk per byte of live data. B-trees fragment and leave partially-empty pages; LSM keeps stale versions until compaction reclaims them.

| Property | B-tree | LSM-tree |
|---|---|---|
| Write path | in-place page update + WAL | append memtable + WAL, flush SSTable |
| On-disk mutability | mutable pages | immutable SSTables |
| Write amplification | moderate (page rewrites) | tunable; leveled higher, tiered lower |
| Read amplification | low, predictable | memtable + N SSTables, cut by Bloom filters |
| Space amplification | fragmentation / half-full pages | stale versions until compaction |
| Range scans | excellent (sorted leaves) | good (merge sorted runs) |
| Latency profile | steady | spiky (compaction stalls) |
| In the wild | PostgreSQL, InnoDB | RocksDB, Cassandra, LevelDB, ScyllaDB |

## A concrete LSM walk-through

**Write.** `PUT user:42 = {plan:pro}` arrives. It is (1) appended to the WAL, then (2) inserted into the memtable's skip list. The client gets an ack — no SSTable was touched. Fast, sequential, durable.

**Flush.** After ~64 MB of writes the memtable is full. It becomes immutable, a fresh empty memtable takes new writes, and a background thread writes the frozen one out as a sorted SSTable in **L0**, complete with its Bloom filter and a sparse block index. The corresponding WAL segment can now be discarded.

**Compaction.** L0 accumulates a few overlapping SSTables. When it crosses a threshold, compaction merge-sorts them with the overlapping key range in L1, keeping the newest value per key, dropping tombstoned and shadowed entries, and writing new non-overlapping L1 SSTables. Later, L1→L2 does the same at 10× scale. This is where write amplification is spent — and where latency spikes if compaction can't keep up with ingest.

**Read.** `GET user:42`: check the active memtable (newest data) → check the immutable memtables being flushed → then walk SSTables newest-to-oldest. For each SSTable, **ask its Bloom filter first**; on "definitely not present," skip the file entirely. On a maybe, binary-search the block index, read the one data block, and return the first (newest) value found — stopping there, since newer shadows older.

## When to pick which

- **Write-heavy ingest** — time-series, event logs, metrics, high-cardinality upserts: **LSM**. Sequential flushes plus tunable compaction absorb write volume a B-tree's random page updates cannot, and you can dial compaction toward write-cheap tiering.
- **Read-heavy, range-scan-heavy, latency-sensitive** — OLTP with secondary indexes, reporting queries, anything needing *predictable* p99: **B-tree**. One value per key, no background compaction to stall a query, and sorted leaves make ranges trivial.
- **The honest caveat:** modern engines blur the line — RocksDB tunes across the whole amplification triangle, and B-tree engines add compression. The interview answer is not "LSM is faster" — it's "which amplification can this workload afford to pay?"

**Try next:** trace a `DELETE` through an LSM-tree and explain how a tombstone can resurrect a key if compaction and a stale replica race — then connect it to why range tombstones exist.
