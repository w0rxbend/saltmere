---
title: 'LSM-Trees vs B-Trees: The Two Storage Engines Under Every Database'
date: 2026-08-10
track: distributed-systems
summary: Online transaction processing (OLTP) databases pick one of two on-disk shapes. B-trees update pages in place, keep everything sorted for cheap reads and range scans, and lean on a write-ahead log for crash safety — the engine under PostgreSQL and InnoDB. LSM-trees buffer writes in a memtable, flush immutable sorted SSTables, and merge them in the background — the engine under RocksDB, Cassandra, and LevelDB. This article walks the mechanics of both, the three amplifications (write, read, space) each one trades, a concrete LSM write→flush→compaction and read path, and the selection criteria.
reading_time: 9
tags:
- lsm-tree
- b-tree
- storage-engines
- databases
- compaction
- rocksdb
- bloom-filter
sources:
- title: Designing Data-Intensive Applications, Ch.3 (Kleppmann) — Storage and Retrieval
  url: https://dataintensive.net/
- title: The Log-Structured Merge-Tree (O'Neil, Cheng, Gawlick, O'Neil, Acta Informatica 1996)
  url: https://www.cs.umb.edu/~poneil/lsmtree.pdf
- title: Compaction — facebook/rocksdb Wiki
  url: https://github.com/facebook/rocksdb/wiki/Compaction
- title: Leveled Compaction — facebook/rocksdb Wiki
  url: https://github.com/facebook/rocksdb/wiki/Leveled-Compaction
- title: Read, write & space amplification — B-Tree vs LSM (Mark Callaghan, Small Datum)
  url: http://smalldatum.blogspot.com/2015/11/read-write-space-amplification-b-tree.html
- title: The Log-Structured Merge-Tree (LSM-Tree) — O'Neil, Cheng, Gawlick, O'Neil (Acta Informatica, 1996)
  url: https://dl.acm.org/doi/10.1007/s002360050048
- title: 'Designing Access Methods: The RUM Conjecture — Athanassoulis et al. (EDBT 2016)'
  url: https://stratos.seas.harvard.edu/publications/designing-access-methods-rum-conjecture
- title: 'Compaction Series: Space Amplification in Size-Tiered Compaction (ScyllaDB)'
  url: https://www.scylladb.com/2018/01/17/compaction-series-space-amplification/
---

**Gist.** Both storage engine families solve one problem: durably persist sorted key-value pairs on a device that is slow at random input/output (I/O). The **B-tree** keeps data in fixed-size pages and mutates them in place, which makes a point lookup a fixed small number of page reads but turns every logical write into a random-access rewrite of a whole page, on top of a sequential write-ahead log record. The **log-structured merge-tree (LSM-tree)** never mutates a written file — it appends to an in-memory buffer, flushes immutable sorted files, and merges them later — which makes writes sequential but pays for it in background compaction work, extra files to probe on read, and stale versions occupying disk until compaction reclaims them.

## The B-tree: update in place

A B-tree stores keys in fixed-size **pages** (commonly 4–16 KB; PostgreSQL defaults to 8 KB, InnoDB to 16 KB), arranged as a balanced tree. One page is the root; interior pages hold key ranges and pointers to child pages; leaf pages hold the values. A read starts at the root and follows pointers down — `O(log n)` page reads, and because the tree is shallow (a 4-level tree of 4 KB pages indexes hundreds of gigabytes), that is a handful of seeks.

A write locates the leaf page for the key and **overwrites it in place**. If the page is full it splits in two and the parent is updated to point at both. This in-place mutation is the defining trait and the source of the hazard: **a split touches multiple pages, and a crash part-way through leaves a tree with a dangling pointer**. B-tree engines defend against this with a **write-ahead log (WAL)** — an append-only file where every modification is recorded *before* the pages are touched, so recovery replays the log to restore a consistent tree. (The same write-ahead discipline underlies [durable outbox and write-behind patterns](/articles/distributed-systems/2026-08-10-cache-database-consistency-dual-write) elsewhere in the corpus.) PostgreSQL and MySQL's InnoDB are B-tree engines.

The resulting read profile is predictable: **every key lives in exactly one place**, so a point lookup costs a fixed, small number of page reads and a **range scan** is a sequential walk across sorted leaves. The write profile is the opposite. Updating one 200-byte row can dirty a whole 4 KB page plus its WAL record, and a page split rewrites several — the origin of a B-tree's **write amplification**.

## The LSM-tree: buffer, flush, merge

The LSM-tree, introduced by O'Neil, Cheng, Gawlick and O'Neil in 1996, avoids random writes entirely. It has three moving parts:

1. **Memtable** — an in-memory sorted structure (a red-black tree or a skip list). Every write enters here first and remains sorted.
2. **WAL** — before a write reaches the memtable it is appended to an on-disk log, so a crash cannot lose the not-yet-flushed memtable.
3. **SSTables** (sorted string tables) — when the memtable reaches a size threshold it is flushed to disk as an **immutable, sorted** file. Once written, an SSTable is never modified.

An update to an existing key does not locate and rewrite the old value; it writes a new entry to the memtable. A delete writes a **tombstone**, a marker recording the absence. The system therefore accumulates many SSTables, each internally sorted but overlapping in key range, with newer files shadowing older ones. The invariant that keeps reads correct is **recency ordering**: for a given key the first value found scanning newest-to-oldest is the live one, and a tombstone found first means the key is absent.

Left alone, that accumulation degrades reads and wastes space on dead versions. **Compaction** is the correction: background threads merge SSTables, keep the newest value per key, drop shadowed values and tombstones, and write fresh consolidated SSTables. RocksDB, Cassandra, LevelDB and ScyllaDB all run this loop.

### Leveled versus size-tiered compaction

How SSTables are organised is the dominant tuning knob, and it maps directly onto the amplification trade.

- **Size-tiered / universal**: SSTables of similar size are merged into a larger one, and each level holds several overlapping runs. A given byte is rewritten once per size tier it passes through rather than once per overlapping file, so ingest is cheap — but a key can live in many runs, so reads and space suffer: dead data lingers, and a large merge **transiently needs room for both its inputs and its output**.
- **Leveled** (the LevelDB and RocksDB default): each level `L` is capped at roughly 10× the size of `L-1`, and within a level SSTables are non-overlapping — one sorted run per level, so **a key exists in at most one file per level**. This minimises space and read amplification at the cost of higher write amplification, because promoting a file into level `L` rewrites the files it overlaps there. The RocksDB wiki describes the trade in the same terms: leveled compaction targets low space amplification and pays in write amplification, while tiered (universal) compaction targets low write amplification and pays in space and read amplification. RocksDB itself is a hybrid — L0 is tiered, deeper levels leveled.

### Bloom filters bound read amplification

A read may have to consult every SSTable that could hold the key. To avoid touching files that cannot, each SSTable carries a **Bloom filter**, a probabilistic set-membership structure. The filter is queried first; a "definitely not present" answer skips the file with no disk read. Bloom filters have **no false negatives** — they never hide a key that exists — but do produce false positives, so occasional wasted reads remain. This is the membership-guard role covered in [Bloom vs cuckoo filters](/articles/distributed-systems/2026-08-10-cuckoo-filters-vs-bloom); cuckoo filters add deletion support, which matters when SSTables appear and disappear under compaction.

## The three amplifications and the RUM conjecture

Every engine trades three quantities; Mark Callaghan's framing on *Small Datum* is the standard reference.

- **Write amplification** — bytes written to disk per byte of logical write. B-trees pay it in page rewrites plus WAL records; leveled LSM compaction pays it re-merging levels.
- **Read amplification** — I/O operations per logical read. B-tree: approximately the tree height. LSM: memtable plus potentially several SSTables, reduced by Bloom filters and by leveling.
- **Space amplification** — bytes on disk per byte of live data. B-trees fragment and leave partially-empty pages; LSM-trees retain stale versions until compaction reclaims them.

Two of the three can be improved by paying in the third, which is the content of the **RUM conjecture** (Athanassoulis et al., EDBT 2016): read, update and memory overheads cannot all be minimised simultaneously.

| | Write amplification | Read amplification | Space amplification |
|---|---|---|---|
| **B-tree** | High (random page rewrites + WAL) | Low (one traversal) | Low–moderate (fragmentation; pages left partially filled by splits) |
| **LSM leveled** | High (repeated re-merges) | Moderate (probe several levels) | Low (RocksDB keeps ~90% of data in the last level) |
| **LSM size-tiered** | Low | High (many overlapping runs) | High (a large merge transiently needs room for inputs plus output; overwrite-heavy workloads are worse still) |

The LSM rows are not one engine: the compaction strategy selects the position on the curve.

| Property | B-tree | LSM-tree |
|---|---|---|
| Write path | in-place page update + WAL | append memtable + WAL, flush SSTable |
| On-disk mutability | mutable pages | immutable SSTables |
| Read amplification | low, predictable | memtable + N SSTables, cut by Bloom filters |
| Range scans | excellent (sorted leaves) | good (merge sorted runs) |
| Latency profile | steady | spiky (compaction stalls) |
| In the wild | PostgreSQL, InnoDB | RocksDB, Cassandra, LevelDB, ScyllaDB |

## A concrete LSM walk-through

**Write.** `PUT user:42 = {plan:pro}` arrives. It is appended to the WAL, then inserted into the memtable's skip list. The client is acknowledged; no SSTable was touched.

**Flush.** Once the memtable reaches its size threshold it is marked immutable, a fresh empty memtable accepts new writes, and a background thread writes the frozen one out as a sorted SSTable in **L0**, together with its Bloom filter and a sparse block index. The corresponding WAL segment can then be discarded.

**Compaction.** L0 accumulates several overlapping SSTables. On crossing a threshold, compaction merge-sorts them with the overlapping key range in L1, keeps the newest value per key, drops tombstoned and shadowed entries, and writes new non-overlapping L1 SSTables. L1→L2 repeats the operation at the next size ratio. This is where write amplification is spent, and where **latency spikes appear when compaction cannot keep pace with ingest**.

**Read.** `GET user:42` checks the active memtable, then the immutable memtables awaiting flush, then walks SSTables newest-to-oldest. For each SSTable the Bloom filter is consulted first; on "definitely not present" the file is skipped. Otherwise the block index is binary-searched, the single data block is read, and the first value found is returned — the scan stops there, because newer shadows older.

### Implementation sketch (Scala)

The load-bearing idea is the recency-ordered read path: newest-first traversal with an early stop, and a tombstone treated as a found value that resolves to absence.

```scala
enum Entry:
  case Value(bytes: Array[Byte])
  case Tombstone

/** One immutable, sorted run. `mayContain` is the Bloom filter probe:
  * false means definitely absent, true means possibly present. */
trait SSTable:
  def mayContain(key: String): Boolean
  def lookup(key: String): Option[Entry]   // reads one data block

final class LsmReadPath(
    active: collection.Map[String, Entry],        // memtable
    flushing: List[collection.Map[String, Entry]], // immutable memtables
    levels: List[List[SSTable]]                   // newest level first
):
  def get(key: String): Option[Array[Byte]] =
    val fromMemory = (active :: flushing).view.flatMap(_.get(key))
    val fromDisk = levels.view.flatten
      .filter(_.mayContain(key))   // Bloom probe avoids the block read
      .flatMap(_.lookup(key))
    // `headOption` is the early stop: the first hit is the live version,
    // so no older run is consulted at all.
    (fromMemory ++ fromDisk).headOption.flatMap:
      case Entry.Value(bytes) => Some(bytes)
      case Entry.Tombstone    => None
```

## Selection criteria

- **Write-heavy ingest** — time-series, event logs, metrics, high-cardinality upserts: **LSM**. Sequential flushes plus tunable compaction absorb write volume that random page updates cannot, and compaction can be tuned toward write-cheap tiering.
- **Read-heavy, range-scan-heavy, latency-sensitive** — OLTP with secondary indexes, reporting queries, anything requiring a predictable p99: **B-tree**. One value per key, no compaction backlog able to throttle writes, and sorted leaves make ranges cheap. Background work does not vanish — checkpoints and PostgreSQL's vacuum of dead row versions still run — but it is not on the critical path of a merge that must complete before ingest can continue.
- Modern engines blur the line: RocksDB is tunable across the whole amplification triangle, and B-tree engines add compression. The operative question is not which family is faster but **which amplification the workload can afford to pay**.

## Pitfalls

- **Tuning a leveled LSM for ingest throughput without watching compaction debt.** Symptom: write latency is flat, then stalls for seconds. Cause: L0 file count crosses the engine's threshold and writes are throttled or stopped because compaction has fallen behind ingest.
- **Assuming a size-tiered compaction needs only the free space of its output.** Symptom: the disk fills during a large merge on a volume that looked half empty. Cause: input runs must remain readable until the output is complete, so the peak requirement is inputs plus output.
- **Treating a deleted key as space reclaimed.** Symptom: disk usage does not fall after a bulk delete. Cause: a delete writes a tombstone; the shadowed values persist until a compaction merges the run holding them with the run holding the tombstone.
- **Expecting a Bloom filter to make a range scan cheap.** Symptom: point lookups are fast, scans are not. Cause: the filter answers membership for a single key, so a range must merge across the candidate runs rather than skipping them.
- **Reading a B-tree's steady write latency as low write cost.** Symptom: an ingest-heavy workload saturates the device far below the expected row rate. Cause: a single small row update writes a whole page plus its WAL record, and a page split writes several.
- **Comparing engines on one amplification.** Symptom: a benchmark declares a winner that regresses in production. Cause: the RUM conjecture — an engine configuration that improves two of read, update and memory overhead pays in the third, so a single-axis benchmark measures the tuning, not the engine.
