---
title: "Apache Iceberg: what a table format actually does"
date: 2026-08-15
track: sys-patterns
summary: "Iceberg replaces 'a table is a directory of files' with a metadata tree of snapshots, manifest lists, and manifests — and gets ACID commits, time travel, and hidden partitioning out of one atomic pointer swap on a catalog. With the v3 spec ratified in 2025 (deletion vectors, row lineage) and AWS, Snowflake, and Databricks all converging on it, it has become the de facto open table layer."
reading_time: 6
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

For fifteen years, a "table" on a data lake meant a Hive-style convention: a directory, subdirectories per partition (`dt=2026-08-15/`), and whatever files happen to be inside. The database's job — knowing what data the table contains — was outsourced to a filesystem listing. Apache Iceberg's core move is to make table state *explicit metadata* instead of *implicit directory layout*, and nearly everything it offers falls out of that one decision.

## What's wrong with a directory

Hive-style tables fail in ways every data engineer has been paged for:

- **No atomicity.** A job writing 500 files that dies at file 300 leaves a half-visible table. Readers see partial writes; there is no isolation between a writer and concurrent readers.
- **Listing is the query planner.** Planning means recursively listing directories — slow on object stores, and historically wrong on eventually-consistent S3.
- **Partitioning is a leaky API.** Consumers must know the layout and filter on the physical partition column (`WHERE dt = '2026-08-15'`); query the timestamp instead and you get a full scan. Changing the partition scheme means rewriting the table and every downstream query.

## The metadata tree

Iceberg tracks a table as a persistent, immutable tree, per the [spec](https://iceberg.apache.org/spec/):

```text
catalog entry ──> table metadata file (schema, partition specs, snapshot log)
                    └─ snapshot ──> manifest list (one per snapshot)
                                      └─ manifest files (partition ranges, stats)
                                            └─ data / delete files (Parquet, ...)
```

A **snapshot** is the complete state of the table at a commit. Its **manifest list** records which manifests belong to it, with partition-range summaries; each **manifest** lists data files with per-file column stats (min/max, null counts). Planning a query is now metadata reads, not directory listings, and pruning happens twice — manifests skipped by partition range, then files skipped by column stats — before a single data file is opened.

**Commits are an atomic pointer swap.** A writer creates new data files, new manifests, and a new metadata file, then asks the **catalog** to compare-and-swap the table's current-metadata pointer. If another writer got there first, the CAS fails and the writer retries against the new base — optimistic concurrency, serializable by default. That single swap is the entire ACID story: readers hold a snapshot and never see partial writes; old snapshots remain addressable, which is what makes **time travel** free.

## Hidden partitioning and partition evolution

Iceberg partitions by *transform*, not by column: `days(ts)`, `bucket(16, user_id)`, `truncate(4, code)`. The relationship between the source column and the partition value is recorded in metadata, so a query filtering on `ts` prunes partitions automatically — no magic `dt` column, no way for users to accidentally full-scan. And because each data file remembers which partition *spec* wrote it, you can change the spec (say, daily to hourly) and it applies to new data only; old files keep their old layout, and the planner handles both. No table rewrite.

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

The last two lines are operational reality: streaming writers produce small files, so **compaction** (`rewrite_data_files`, plus snapshot expiry to garbage-collect unreferenced files) is a scheduled maintenance job, not an optional nicety. And rollback shows the flip side of time travel — a bad backfill is undone by moving the pointer, not restoring backups.

## Where the ecosystem is (2025–2026)

The strategic story is convergence. AWS launched **S3 Tables** (Dec 2024) — S3 buckets whose native object type *is* an Iceberg table, with managed compaction. Databricks (which acquired Tabular, founded by Iceberg's creators) and Snowflake both now treat Iceberg as a first-class format rather than a competitor. The **REST catalog** protocol decoupled engines from catalog implementations, which is what makes "one copy of data, five query engines" practical. The reference Java implementation is at **1.11.0** as of mid-2026.

The **v3 spec**, approved by the Iceberg community in mid-2025, closes the remaining gaps with warehouses:

- **Deletion vectors** — row-level deletes as Roaring bitmaps, one vector per data file, replacing v2's scattered positional delete files (far fewer files to merge at read time).
- **Row lineage** — stable row IDs plus sequence numbers, so engines can compute row-level diffs between snapshots for CDC and incremental processing.
- **Variant type** for semi-structured JSON-ish data, **geometry/geography** types, nanosecond timestamps, and default column values.

Vendor adoption is tracking the spec: AWS shipped v3 deletion-vector and row-lineage support in late 2025, and Snowflake took v3 support GA in May 2026. When Databricks, Snowflake, AWS, and Google all implement the same open spec, the table format has effectively become the storage-layer contract of the lakehouse.

| | Hive-style table | Iceberg |
|---|---|---|
| Table state | directory listing | metadata tree + catalog pointer |
| Commit | none (file moves) | atomic CAS, optimistic retry |
| Partitioning | physical columns, user-visible | hidden transforms, evolvable |
| Query planning | list + name filters | manifest pruning + file stats |
| Row-level deletes | rewrite partitions | delete files / v3 deletion vectors |
| History | none | snapshots, time travel, rollback |

**Try next:** `pip install "pyiceberg[sql-sqlite,pyarrow]"`, create a local SQLite-backed catalog, append two batches to a table, then read `table.history()` and scan with an older `snapshot_id` — the whole snapshot/manifest tree is inspectable as plain JSON and Avro files on disk.
