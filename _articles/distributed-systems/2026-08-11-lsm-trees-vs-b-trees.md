---
title: "LSM-Trees vs B-Trees: The Storage-Engine Trade-off"
date: 2026-08-11
track: distributed-systems
summary: "Why B-trees update in place and LSM-trees never do, how the three amplifications — write, read, space — get traded against each other, and what leveled vs size-tiered compaction actually buys you."
reading_time: 6
tags: [storage-engines, lsm-tree, b-tree, compaction, rocksdb, bloom-filter]
sources:
  - title: "The Log-Structured Merge-Tree (LSM-Tree) — O'Neil, Cheng, Gawlick, O'Neil (Acta Informatica, 1996)"
    url: "https://dl.acm.org/doi/10.1007/s002360050048"
  - title: "Designing Access Methods: The RUM Conjecture — Athanassoulis et al. (EDBT 2016)"
    url: "https://stratos.seas.harvard.edu/publications/designing-access-methods-rum-conjecture"
  - title: "Leveled Compaction (RocksDB Wiki)"
    url: "https://github.com/facebook/rocksdb/wiki/Leveled-Compaction"
  - title: "Compaction (RocksDB Wiki)"
    url: "https://github.com/facebook/rocksdb/wiki/Compaction"
  - title: "Compaction Series: Space Amplification in Size-Tiered Compaction (ScyllaDB)"
    url: "https://www.scylladb.com/2018/01/17/compaction-series-space-amplification/"
---

"Should this use Postgres or Cassandra?" is really a question about storage engines, and the interviewer usually wants you to reach the B-tree versus LSM-tree distinction on your own. Both give you an ordered key-value map with `get`, `put`, and range scans. They differ in one decision: **does a write modify data in place, or only ever append?** Everything else — the amplification profile, the compaction knobs, which real database wins which workload — falls out of that.

## The B-tree write path: update in place

A B-tree stores keys in fixed-size pages (InnoDB and Postgres use 8–16 KB) arranged as a shallow, high-fanout tree. To write a key, you walk from the root to the leaf page that owns its range, then modify that page *where it already lives on disk*.

Two consequences follow. First, a crash mid-write can tear a page, so the engine writes the change to a **write-ahead log** (redo log in InnoDB, WAL in Postgres) and fsyncs *that* before touching the page — the log is the durability boundary, the page update is lazy. Second, keys arrive in whatever order the application produces them, so leaf updates scatter across the disk as small random writes. Even changing one row rewrites a whole page (and, on split, its parent). That is B-tree **write amplification**: bytes-to-storage far exceed bytes-from-application, and the writes are random.

This is exactly where index locality matters. A monotonic key — an autoincrement column, a time-ordered ID — always lands in the right-most leaf, so inserts stay sequential and pages fill densely. A random key (a UUIDv4, a hash) spreads inserts across every leaf, dirtying far more pages and fragmenting the tree. Choosing sequential IDs is partly a storage-engine optimization.

The payoff: reads are cheap and predictable. One key is one root-to-leaf traversal, a handful of page reads, no merging. B-trees are **read-optimized**.

## The LSM-tree write path: append and sort later

The log-structured merge-tree (O'Neil et al., 1996) refuses in-place updates entirely. A write goes to two places: an append to a WAL for durability, and an insert into an in-memory sorted structure, the **memtable** (typically a skip list). No disk seek, no page read.

When the memtable fills, it becomes immutable, a fresh empty one takes over, and the full one is flushed to disk as a **sorted string table (SSTable)** — one sequential write of an already-sorted file. Old values are never overwritten; a newer write to the same key simply lands in a later SSTable, and a delete writes a **tombstone**. Because every disk write is sequential and batched, LSM-trees are **write-optimized** — the property that makes Cassandra, ScyllaDB, and RocksDB good at write-heavy ingest.

The cost is that one key may now exist in many files, so a background process must periodically merge SSTables, drop shadowed values and tombstones, and keep read cost bounded. That process is **compaction**, and it is where the real design tension lives.

## The three amplifications and the RUM conjecture

Storage engines are judged on three overheads. You can improve two by paying in the third — the point of the **RUM conjecture** (Athanassoulis et al., EDBT 2016): read, update, and memory overheads cannot all be minimized at once.

| | Write amplification | Read amplification | Space amplification |
|---|---|---|---|
| **B-tree** | High (random page rewrites + WAL) | Low (one traversal) | Low–moderate (fragmentation, ~⅔ page fill) |
| **LSM leveled** | High (repeated re-merges) | Moderate (probe several levels) | Low (RocksDB keeps ~90% of data in the last level) |
| **LSM size-tiered** | Low | High (many overlapping runs) | High (up to ~2× transient, worse on overwrites) |

Note the LSM row isn't one thing — the compaction strategy picks where on the curve you sit.

## The LSM read path

A `get` may have to check the memtable and every SSTable, so LSM-trees lean on three tricks: **bloom filters** per SSTable to skip files that definitely lack the key, a **block cache** for hot data, and sorted layout so a present key is a binary search within one block.

```
def get(key):
    if key in memtable:                 # newest data, in RAM
        return memtable[key]            # (may be a tombstone -> "not found")
    for sstable in newest_to_oldest():  # order matters: first hit wins
        if not sstable.bloom.may_contain(key):
            continue                    # skip file with ~1% false-positive cost
        block = sstable.index.locate(key)
        val = block_cache.get_or_read(block).search(key)
        if val is not None:
            return val                  # includes tombstones
    return NOT_FOUND
```

The bloom check is what makes point reads viable — without it, a read touches every level on disk. (There's no standalone bloom-filter article in this journal yet; the [Merkle-tree piece](/articles/distributed-systems/2026-07-27-merkle-trees-anti-entropy) uses the same "cheap probabilistic summary to avoid moving real data" idea for anti-entropy.)

## Compaction: leveled vs size-tiered

**Size-tiered (STCS)** groups SSTables of similar size and merges a tier once enough accumulate. Merges are infrequent, so write amplification is low — but overlapping runs pile up, so a key can live in many files (read amplification) and old versions linger. ScyllaDB measured STCS at roughly **2× space amplification transiently** (inputs can't be deleted until the merged output is written) and far worse under repeated overwrites, which is why the rule of thumb is *keep half the disk free*.

**Leveled (LCS)** keeps each level (except L0) as non-overlapping SSTables sized ~10× the level above. A key appears in at most one file per level, so reads and space stay tight — RocksDB keeps about 90% of data in the last level — but data is re-merged as it cascades down, pushing write amplification often **above 10×**.

```
# RocksDB: choose leveled, 10x per level, 256MB L1
options.compaction_style = kCompactionStyleLevel
options.max_bytes_for_level_multiplier = 10
options.max_bytes_for_level_base = 256 * 1024 * 1024
options.level_compaction_dynamic_level_bytes = true  # stabilizes space amp
```

The choice is a direct RUM trade: STCS buys write throughput with space and read cost; LCS buys read and space efficiency with write cost.

So the interview answer: **read-heavy, in-place, predictable latency → B-tree** (InnoDB, Postgres, BoltDB). **Write-heavy, high ingest, tunable via compaction → LSM-tree** (RocksDB/LevelDB, Cassandra, ScyllaDB). Then name your amplification and defend the trade.

**Try next:** set `compaction_style` to size-tiered on a RocksDB instance, run a write-then-overwrite loop, and watch `rocksdb.compaction.stats` — the space-amplification spike is exactly the STCS behavior above.
