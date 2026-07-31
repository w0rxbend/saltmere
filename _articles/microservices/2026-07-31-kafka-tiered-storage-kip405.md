---
title: "Kafka tiered storage (KIP-405): offload cold segments to object storage"
date: 2026-07-31
track: microservices
summary: "KIP-405 splits a Kafka log into a hot local tier and a cold remote tier on S3 or GCS, so brokers keep only recent data on disk. This decouples storage from compute, shrinks rebalances, and makes month-long retention cheap. Here is how the local/remote split works, the config that turns it on, and the plugin interface behind it."
reading_time: 5
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

Retention is where Kafka's economics fall apart. A broker's local disk holds every segment of every partition it leads, so keeping 30 days of a busy topic means provisioning 30 days of fast SSD on every replica — most of which is never read after the first few minutes. Worse, all that data has to move during a reassignment: add a broker or replace a failed one and the cluster copies terabytes of cold segments over the network before the new replica is in sync. Storage and compute are welded together.

**KIP-405 (Tiered Storage)** breaks the weld. It reached production-ready status in **Apache Kafka 3.9.0**, released **6 November 2024**, after shipping as Early Access in 3.6.0. Brokers now keep only recent "hot" data locally and offload older segments to an external object store.

## The local tier and the remote tier

Each partition's log is split into two tiers. The **local tier** is the broker's disk, exactly as before, but it now holds only a rolling window of recent segments. The **remote tier** is external storage — S3, GCS, Azure Blob, HDFS — that holds everything older.

The controls are new retention knobs that sit *underneath* the familiar ones:

- `retention.ms` / `retention.bytes` — the total retention across both tiers. A segment older than this is deleted from remote storage permanently.
- `local.retention.ms` / `local.retention.bytes` — how long a segment stays on the broker's local disk *after* it has been copied to the remote tier. Once it exceeds this, the local copy is dropped but the remote copy survives until the outer `retention.*` limit.

So you might set `retention.ms=2592000000` (30 days, all in the remote tier) but `local.retention.ms=3600000` (1 hour on disk). Consumers reading the tail hit local disk; a consumer replaying from last week transparently fetches from object storage. Active (unclosed) segments always stay local — only rolled, closed segments are eligible for upload. Note that tiered storage does **not** support compacted topics; enabling it on one throws a configuration exception.

## Turn it on

Tiered storage is enabled at the broker level, then per topic. On the broker (`server.properties`) you enable the subsystem and wire up the two plugins:

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

Then flip it on for a topic — nothing moves to remote storage until you do:

```bash
bin/kafka-configs.sh --bootstrap-server localhost:9092 --alter \
  --topic orders \
  --add-config 'remote.storage.enable=true,\
local.retention.ms=3600000,\
retention.ms=2592000000'
```

That topic now keeps one hour on local disk and thirty days in the bucket.

## What actually moves the bytes

Inside each broker, the **RemoteLogManager (RLM)** is the orchestrator. It watches leader partitions, copies eligible segments to the remote tier, expires them per the retention rules, and serves fetch requests that fall below the local log-start offset. The RLM delegates to two pluggable interfaces you (or a vendor) implement:

- **`RemoteStorageManager` (RSM)** — the data plane. It knows how to `copyLogSegmentData`, `fetchLogSegment`, and `deleteLogSegmentData` against a specific backend. This is where the S3/GCS SDK calls live.
- **`RemoteLogMetadataManager` (RLMM)** — the metadata plane. It records, for every remote segment, its offset range, leader epoch, and storage location so the broker can find it later. The built-in `TopicBasedRemoteLogMetadataManager` stores this in an internal `__remote_log_metadata` topic.

Because the interfaces are open, the same broker can front S3 in one deployment and HDFS in another without touching Kafka's core. Kafka 3.9 also hardened operations around this: KIP-950 lets you disable tiered storage on a topic dynamically, and KIP-956 adds read/write quotas so a burst of remote fetches can't saturate a broker's bandwidth.

The payoff is architectural. Reassignments only copy the small local window instead of the full history, so scaling out and recovering from broker loss go from hours to minutes. Long retention becomes an object-storage bill instead of a fleet of oversized SSDs — and storage capacity stops dictating how many brokers you run.

**Try next:** Spin up a single-broker Kafka 3.9 with `remote.log.storage.system.enable=true` using the bundled `LocalTieredStorage` test implementation (`remote.log.storage.manager.class.name=org.apache.kafka.server.log.remote.storage.LocalTieredStorage`), create a topic with `remote.storage.enable=true`, `local.retention.ms=1000`, `segment.bytes=1048576`, produce a few MB, and watch segments appear in the configured `rsm.config.dir` while `kafka-log-dirs.sh` shows the local log shrinking.
