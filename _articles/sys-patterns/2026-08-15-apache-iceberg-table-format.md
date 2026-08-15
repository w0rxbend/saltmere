---
title: "Apache Iceberg: what a table format does"
date: 2026-08-15
track: sys-patterns
summary: "Iceberg replaces 'a table is a directory of files' with a metadata tree of snapshots, manifest lists, and manifests — and derives ACID commits, time travel, and hidden partitioning from one atomic pointer swap on a catalog. With the v3 spec ratified in 2025 (deletion vectors, row lineage) and AWS, Snowflake, and Databricks converging on it, it has become the de facto open table layer."
reading_time: 7
tags: [iceberg, table-formats, data-lake, lakehouse, acid, time-travel]
sources:
  - title: "Apache Iceberg — Table Spec (v1/v2/v3)"
    url: "https://iceberg.apache.org/spec/"
  - title: "Google Open Source Blog — What's new in Apache Iceberg v3? (Aug 2025)"
    url: "https://opensource.googleblog.com/2025/08/whats-new-in-iceberg-v3.html"
  - title: "Databricks — Apache Iceberg v3: Moving the Ecosystem Towards Unification"
    url: "https://www.databricks.com/blog/apache-icebergtm-v3-moving-ecosystem-towards-unification"
  - title: "Amazon — S3 Tables: managed Apache Iceberg tables (Dec 2024 announcement)"
    url: "https://press.aboutamazon.com/2024/12/amazon-s3-expands-capabilities-with-managed-apache-iceberg-tables-for-faster-data-lake-analytics-and-automatic-metadata-generation-to-simplify-data-discovery-and-understanding"
  - title: "Snowflake release notes — Apache Iceberg v3 support GA (May 2026)"
    url: "https://docs.snowflake.com/en/release-notes/2026/other/2026-05-07-iceberg-v3-ga"
---

**Gist.** A Hive-style table on a data lake is a directory whose contents define the table, so table state is whatever a filesystem listing returns at that instant — no atomic commit, no isolation, no history. Apache Iceberg makes table state **explicit metadata**: an immutable tree of snapshot, manifest list, manifest, and data files, whose root pointer is swapped atomically by a catalog under compare-and-swap (CAS). The cost is that every commit writes new metadata files and every table accumulates obsolete snapshots and small files, so **compaction and snapshot expiry become mandatory scheduled maintenance** rather than optional tuning.

## The failure modes of a directory

Hive-style tables fail in three distinct ways:

- **No atomicity.** A job writing 500 files that dies at file 300 leaves 300 visible files. There is no boundary between a partial write and a complete one, and therefore no isolation between a writer and concurrent readers.
- **Listing is the query planner.** Planning requires recursively listing directories — expensive on object stores, and historically incorrect on eventually-consistent S3, where a listing could omit a file that had already been written.
- **Partitioning is a leaky abstraction.** Consumers must know the physical layout and filter on the partition column (`WHERE dt = '2026-08-15'`); a filter on the underlying timestamp instead yields a full scan. Changing the partition scheme requires rewriting the table and every downstream query.

## The metadata tree

Iceberg tracks a table as a persistent, immutable tree, per the [spec](https://iceberg.apache.org/spec/):

```text
catalog entry ──> table metadata file (schema, partition specs, snapshot log)
                    └─ snapshot ──> manifest list (one per snapshot)
                                      └─ manifest files (partition ranges, stats)
                                            └─ data / delete files (Parquet, ...)
```

A **snapshot** is the complete state of the table at one commit. Its **manifest list** records which manifests belong to that snapshot together with partition-range summaries; each **manifest** lists data files with per-file column statistics (minimum and maximum values, null counts). Planning therefore reads metadata rather than listing directories, and pruning occurs at two levels — **manifests eliminated by partition range, then individual files eliminated by column statistics** — before any data file is opened.

Every node in the tree is immutable. A commit does not modify existing manifests; it writes new ones and reuses the unchanged ones by reference. This is what makes an old snapshot remain readable after a newer commit: its manifest list still names files that no process has overwritten.

## The commit protocol

**A commit is an atomic pointer swap.** The sequence is fixed: the writer stages data files, then writes the manifests that describe them, then the manifest list, then a new table metadata file — all under fresh, unique names — and only then asks the **catalog** to compare-and-swap the table's current-metadata pointer from the base version the writer read to the new version.

The invariant enforced by the CAS is that **the pointer only advances from the exact metadata version the writer planned against**. If a concurrent writer committed in the interim, the CAS fails; the losing writer re-reads the new base, re-validates its operation against it, and retries. This is optimistic concurrency control, and it is the whole of the ACID story:

- **Atomicity and isolation** — a reader resolves the pointer once and reads a fixed snapshot; files written by an in-flight commit are unreferenced and therefore invisible.
- **Durability** — the data files exist before the pointer names them, never after.
- **Consistency** — validation on retry rejects operations whose preconditions the winning commit invalidated.

The failure mode this creates is **write amplification under contention**: a writer that repeatedly loses the CAS re-plans repeatedly, and a slow commit competing with fast ones can starve. The correctness of the whole scheme also rests entirely on the catalog: **if the catalog cannot provide an atomic compare-and-swap on the pointer, Iceberg's guarantees do not hold.**

A failed or abandoned commit leaves its staged data files on storage with nothing referencing them. They are not visible to readers, but they are not reclaimed either until an orphan-file cleanup runs.

### Implementation sketch (Scala)

The load-bearing idea is the retry loop around the catalog CAS. The catalog interface below stands in for whatever backing store holds the pointer; the point is that `compareAndSet` is the only operation that must be atomic.

```scala
final case class MetadataLocation(uri: String)

trait Catalog:
  def current(table: String): MetadataLocation
  /** Atomic: succeeds only if the stored pointer still equals `expected`. */
  def compareAndSet(table: String, expected: MetadataLocation, next: MetadataLocation): Boolean

final case class Plan(base: MetadataLocation, newMetadata: MetadataLocation)

def commit(
    catalog: Catalog,
    table: String,
    plan: MetadataLocation => Plan,   // writes data + manifests, returns new metadata
    validate: (MetadataLocation, Plan) => Boolean,
    maxAttempts: Int = 4
): MetadataLocation =
  @annotation.tailrec
  def attempt(n: Int): MetadataLocation =
    val base = catalog.current(table)
    val p = plan(base)
    // Re-planning against the winner's base is what makes the retry safe:
    // an operation whose preconditions the winner invalidated fails here.
    if !validate(base, p) then throw IllegalStateException("conflicting commit")
    else if catalog.compareAndSet(table, base, p.newMetadata) then p.newMetadata
    else if n >= maxAttempts then throw IllegalStateException(s"lost CAS $n times")
    else attempt(n + 1)
  attempt(1)
```

Files written by an attempt that ends in a throw remain on storage, unreferenced — the orphan case named above.

## Hidden partitioning and partition evolution

Iceberg partitions by *transform* rather than by stored column: `days(ts)`, `bucket(16, user_id)`, `truncate(4, code)`. The relationship between source column and partition value is recorded in metadata, so **a filter on `ts` prunes partitions without the query naming a partition column**. There is no separate `dt` column for a consumer to omit.

Each data file records which **partition spec** produced it. A spec change — daily to hourly, for instance — applies to newly written data only; existing files keep their original layout and the planner evaluates both specs during pruning. No table rewrite is required.

```sql
-- Spark SQL
CREATE TABLE lake.db.events (id BIGINT, ts TIMESTAMP, payload STRING)
USING iceberg PARTITIONED BY (days(ts), bucket(16, id));

INSERT INTO lake.db.events VALUES (1, current_timestamp(), 'hello');

SELECT * FROM lake.db.events.snapshots;         -- inspect commit history

SELECT * FROM lake.db.events
  TIMESTAMP AS OF '2026-08-15 09:00:00';        -- time travel by time
SELECT * FROM lake.db.events
  VERSION AS OF 8158463585037126327;            -- ...or by snapshot id

CALL lake.system.rewrite_data_files(table => 'db.events');   -- compaction
CALL lake.system.rollback_to_snapshot('db.events', 8158463585037126327);
```

The last two statements are the operational consequence of the design. Streaming writers commit frequently and produce small files, so **compaction (`rewrite_data_files`) plus snapshot expiry to garbage-collect unreferenced files is a scheduled maintenance job**. Rollback is the inverse of time travel: a bad backfill is undone by moving the pointer to an earlier snapshot, provided that snapshot has not yet been expired.

## Ecosystem state, 2025–2026

AWS launched **S3 Tables** in December 2024 — S3 buckets whose native object type is an Iceberg table, with managed compaction. Databricks, which acquired Tabular (founded by Iceberg's creators), and Snowflake both treat Iceberg as a first-class format. The **REST catalog** protocol separates engines from catalog implementations, which is what permits one copy of data to be read by several query engines.

The **v3 spec**, published in 2025, adds:

- **Deletion vectors** — row-level deletes encoded as Roaring bitmaps, one vector per data file, in place of v2's scattered positional delete files. The effect is fewer files to merge during read.
- **Row lineage** — stable row identifiers plus sequence numbers, allowing engines to compute row-level differences between snapshots for change data capture (CDC) and incremental processing.
- **Variant** type for semi-structured data, **geometry/geography** types, nanosecond timestamps, and default column values.

Vendor adoption tracks the spec at different rates: Snowflake made v3 support generally available in May 2026. Support elsewhere lands feature by feature rather than all at once, so a table written at v3 is not guaranteed readable by every engine that reads v2.

| | Hive-style table | Iceberg |
|---|---|---|
| Table state | directory listing | metadata tree + catalog pointer |
| Commit | none (file moves) | atomic CAS, optimistic retry |
| Partitioning | physical columns, user-visible | hidden transforms, evolvable |
| Query planning | list + name filters | manifest pruning + file stats |
| Row-level deletes | rewrite partitions | delete files / v3 deletion vectors |
| History | none | snapshots, time travel, rollback |

## Pitfalls

- **A catalog without an atomic compare-and-swap silently voids isolation.** Two writers both observe the same base metadata, both write, and the second overwrites the first's pointer — the first commit's rows disappear while its data files remain on storage.
- **Snapshot expiry deletes the target of a rollback.** Time travel and `rollback_to_snapshot` work only for snapshots still retained; an aggressive expiry policy removes the recovery point before the bad backfill is noticed.
- **Frequent small commits degrade planning as well as scanning.** Each commit adds manifests; a table with many thousands of small manifests spends its planning time reading metadata even when the data scanned is small.
- **Failed commits leave unreferenced data files.** The files are invisible to readers and are not removed by snapshot expiry, which only reclaims files that some expired snapshot referenced; orphan-file cleanup is a separate operation.
- **Long-running writers lose the CAS repeatedly under contention.** A commit that takes minutes to plan against a table receiving second-scale commits re-plans on every attempt and can exhaust its retry budget without ever committing.
- **Partition evolution does not reorganise existing data.** After switching from daily to hourly partitions, queries over historical ranges still prune at day granularity, because the old files carry the old spec.
