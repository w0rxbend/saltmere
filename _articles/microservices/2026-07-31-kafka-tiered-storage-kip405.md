---
title: "Kafka tiered storage (KIP-405): offload cold segments to object storage"
date: 2026-07-31
track: microservices
summary: "KIP-405 splits a Kafka log into a hot local tier and a cold remote tier on object storage, so brokers keep only recent segments on disk. This decouples storage from compute, shrinks reassignments, and moves long retention onto an object-storage bill. The local/remote split, the configuration that enables it, and the two plugin interfaces behind it."
reading_time: 6
tags: [kafka, tiered-storage, kip-405, object-storage, retention, event-streaming]
sources:
  - title: "KIP-405: Kafka Tiered Storage (Apache Kafka wiki)"
    url: "https://cwiki.apache.org/confluence/display/KAFKA/KIP-405%3A+Kafka+Tiered+Storage"
  - title: "Apache Kafka 3.9.0 Release Announcement (Nov 6, 2024)"
    url: "https://kafka.apache.org/blog/2024/11/06/apache-kafka-3.9.0-release-announcement/"
  - title: "Tiered Storage — Apache Kafka 3.9 documentation"
    url: "https://kafka.apache.org/39/operations/tiered-storage/"
  - title: "Apache Kafka Tiered Storage in Depth: How Writes and Metadata Flow (Aiven)"
    url: "https://aiven.io/blog/apache-kafka-tiered-storage-in-depth-how-writes-and-metadata-flow"
---

**Gist.** In a classic Apache Kafka deployment every replica of a partition stores the partition's entire retained log on local disk, so a 30-day retention policy provisions 30 days of fast disk on every replica and forces a reassignment to copy that whole history over the network. **KIP-405 (Tiered Storage)**, production-ready in **Apache Kafka 3.9.0** (released **6 November 2024**) after Early Access in 3.6.0, keeps only a rolling window of recent segments locally and offloads older closed segments to an external object store through two pluggable interfaces. The cost is a second storage system in the read path: historical fetches now traverse the object store and its metadata index rather than the page cache, and the feature is unavailable for compacted topics.

## The local tier and the remote tier

Each partition's log is split into two tiers. The **local tier** is the broker's disk, holding a rolling window of recent segments. The **remote tier** is external storage — Amazon Simple Storage Service (S3), Google Cloud Storage (GCS), Azure Blob Storage, or the Hadoop Distributed File System (HDFS) — holding everything older.

Two layers of retention control the split:

- `retention.ms` / `retention.bytes` — total retention across both tiers. A segment beyond this limit is deleted from remote storage permanently.
- `local.retention.ms` / `local.retention.bytes` — how long a segment remains on local disk *after* it has been copied to the remote tier. Past this limit the local copy is dropped; the remote copy survives until the outer `retention.*` limit expires it.

The ordering is the invariant that matters: **a segment is dropped locally only after a successful remote copy has been recorded**, so `local.retention.*` never deletes the only extant copy. Configuring `retention.ms=2592000000` (30 days) with `local.retention.ms=3600000` (1 hour) yields one hour of log on disk and thirty days in the object store. Consumers reading the tail are served from local disk; a consumer replaying from a week earlier is served from remote storage without any change on the client side.

**Only rolled, closed segments are eligible for upload.** The active segment — the one currently receiving appends — stays local until it rolls. **Tiered storage does not support compacted topics**; enabling it on one raises a configuration exception.

## Enabling it

Tiered storage is enabled at the broker level first, then per topic. The broker configuration turns on the subsystem and names the two plugin classes:

```properties
# server.properties — enable the tiered storage subsystem
remote.log.storage.system.enable=true

# RemoteStorageManager: reads/writes segment data in the object store
remote.log.storage.manager.class.name=com.example.S3RemoteStorageManager
remote.log.storage.manager.impl.prefix=rsm.config.
rsm.config.bucket=my-kafka-tiered-bucket
rsm.config.region=us-east-1

# RemoteLogMetadataManager: tracks which segments live where
remote.log.metadata.manager.class.name=org.apache.kafka.server.log.remote.metadata.storage.TopicBasedRemoteLogMetadataManager
remote.log.metadata.manager.listener.name=PLAINTEXT
```

Nothing is offloaded until a topic opts in:

```bash
bin/kafka-configs.sh --bootstrap-server localhost:9092 --alter \
  --topic orders \
  --add-config 'remote.storage.enable=true,\
local.retention.ms=3600000,\
retention.ms=2592000000'
```

That topic then keeps one hour on local disk and thirty days in the bucket.

## What moves the bytes

Inside each broker the **RemoteLogManager (RLM)** is the orchestrator. It tracks the partitions for which the broker is leader, copies eligible segments to the remote tier, expires them according to the retention rules, and serves fetch requests for offsets below the **local log-start offset** — the boundary that separates "still on disk" from "only in the remote tier". The RLM delegates to two interfaces, either implemented in-house or supplied by a vendor:

- **`RemoteStorageManager` (RSM)** — the data plane. It implements `copyLogSegmentData`, `fetchLogSegment` and `deleteLogSegmentData` against one specific backend; the object-store SDK calls live here.
- **`RemoteLogMetadataManager` (RLMM)** — the metadata plane. For every remote segment it records the offset range, the leader epoch, and the storage location, so the broker can locate the segment that covers a requested offset. The built-in `TopicBasedRemoteLogMetadataManager` stores these records in an internal Kafka topic, `__remote_log_metadata`.

The leader-epoch information is part of the segment metadata record itself, so resolving a remote segment does not depend on the current leader's local state. Because both interfaces are pluggable, one deployment can front S3 and another HDFS without changes to Kafka's core.

Kafka 3.9 added two operational controls around the feature: **KIP-950** allows tiered storage to be disabled on a topic (in 3.9 for ZooKeeper-based clusters), and **KIP-956** adds read and write quotas so that a burst of remote fetches does not saturate a broker's bandwidth.

The consequence is architectural. A reassignment copies only the local window instead of the full history, so scaling out and recovering from a lost broker no longer scale with retention length. Long retention becomes an object-storage cost rather than a fleet of oversized local disks, and storage capacity stops determining broker count.

### Implementation sketch (Scala)

The load-bearing part of an `RemoteStorageManager` is the mapping from a segment's metadata to a stable object key, and the translation of a byte-range fetch into a ranged read. The sketch below shows that mapping only; the real interface has further methods, and index handling, retries and credential plumbing are omitted.

```scala
// Object key must be stable across leader changes: derive it from the
// segment's remote id, not from the local file path or the broker id.
def objectKey(m: RemoteLogSegmentMetadata): String =
  val id = m.remoteLogSegmentId
  val tp = id.topicIdPartition
  s"${tp.topicId}/${tp.partition}/${m.startOffset}-${id.id}.log"

def copyLogSegmentData(
    m: RemoteLogSegmentMetadata,
    data: LogSegmentData
): Unit =
  store.put(objectKey(m), data.logSegment)          // Path to the .log file

// startPosition is a byte offset inside the segment, not a Kafka offset:
// the broker has already resolved the offset via the metadata manager.
def fetchLogSegment(
    m: RemoteLogSegmentMetadata,
    startPosition: Int
): InputStream =
  store.getRange(objectKey(m), startPosition, m.segmentSizeInBytes)

def deleteLogSegmentData(m: RemoteLogSegmentMetadata): Unit =
  store.delete(objectKey(m))                        // Must be idempotent:
                                                    // deletion may be retried
                                                    // after a partial failure.
```

## Pitfalls

- Enabling `remote.storage.enable=true` on a compacted topic fails with a configuration exception; tiered storage and compaction are mutually exclusive.
- Setting `local.retention.ms` without lowering `segment.bytes` leaves the active segment resident for as long as it takes to roll, since an unclosed segment is never eligible for upload — local disk usage then tracks segment roll time, not the configured local retention.
- `retention.ms` governs both tiers, so shortening it deletes data from the remote tier permanently, not only from disk.
- A consumer replaying from the beginning of a long-retention topic reads from the object store rather than the page cache; without the KIP-956 read quotas such a replay competes with tail traffic for broker bandwidth.
- A `deleteLogSegmentData` implementation that is not idempotent can fail on retry after a partial delete, leaving the metadata record and the object out of agreement.
- The metadata plane is itself a Kafka topic when `TopicBasedRemoteLogMetadataManager` is used: `__remote_log_metadata` is a cluster dependency whose availability governs whether remote segments can be located at all.

**Further work:** a single-broker Kafka 3.9 with `remote.log.storage.system.enable=true` and the filesystem-backed test implementation shipped in Kafka's storage test artifact (`remote.log.storage.manager.class.name=org.apache.kafka.server.log.remote.storage.LocalTieredStorage`, which must be on the broker classpath) exercises the full path locally. Create a topic with `remote.storage.enable=true`, `local.retention.ms=1000` and `segment.bytes=1048576`, produce a few megabytes, then observe segments appearing under the configured `rsm.config.dir` while `kafka-log-dirs.sh` reports the local log shrinking.
