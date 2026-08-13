---
title: "Flink 2.x disaggregated state: ForSt puts your keyed state on S3"
date: 2026-08-13
track: sys-patterns
summary: "Flink 2.x (2.3.0 is current as of mid-2026) flips the state model: the ForSt backend makes object storage the primary home of keyed state, with local disk demoted to cache, and an async execution model hides the remote latency. Why that yields near-instant rescaling, sub-3-second checkpoints, and smaller bills — and where the experimental edges still are."
reading_time: 5
tags: [flink, disaggregated-state, forst, checkpointing, stream-processing]
sources:
  - title: "Apache Flink 2.0.0 release announcement"
    url: "https://flink.apache.org/2025/03/24/apache-flink-2.0.0-a-new-era-of-real-time-data-processing/"
  - title: "Apache Flink 2.3.0 release announcement"
    url: "https://flink.apache.org/2026/06/25/apache-flink-2.3.0-release-announcement/"
  - title: "Flink docs — Disaggregated State Management"
    url: "https://nightlies.apache.org/flink/flink-docs-stable/docs/ops/state/disaggregated_state/"
  - title: "Disaggregated State Management in Apache Flink 2.0 (Mei et al., VLDB 2025)"
    url: "https://www.vldb.org/pvldb/vol18/p4846-mei.pdf"
  - title: "Alibaba Cloud — Apache Flink 2.0: Streaming into the Future"
    url: "https://www.alibabacloud.com/blog/apache-flink-2-0-streaming-into-the-future_602008"
---

For a decade, "big state in Flink" meant RocksDB on local disk: fast reads, but state welded to the machine. Rescale a job and terabytes must be redistributed; recover a node and state must be downloaded before a single record flows; checkpoint and the delta must be uploaded on the hot path. In containers it's worse — you provision (and pay for) compute pods sized for their disks. The [VLDB 2025 paper](https://www.vldb.org/pvldb/vol18/p4846-mei.pdf) from the Flink/Alibaba team reports that 35% of Flink jobs in one Alibaba production fleet were disk-bound: buying CPUs to get SSDs.

Flink 2.x's headline answer is **disaggregated state**: object storage (S3/OSS/HDFS) becomes the *primary* store, local disk becomes a cache. Know the release line for interviews: 2.0 (March 2025) introduced it; 2.1 (July 2025) and 2.2 (December 2025) iterated; **2.3.0 (June 2026) is the current stable release**.

## ForSt: the LSM tree moves to object storage

The new backend is **ForSt** ("For Streaming"), a RocksDB descendant whose [LSM tree](/articles/distributed-systems/2026-08-11-lsm-trees-vs-b-trees) reads and writes SST files on a distributed filesystem directly. Local disk (`state.backend.forst.cache.dir`) holds a tiered cache of hot files, so the working set behaves locally while ownership sits remotely. Compaction — CPU-heavy in RocksDB — can be pushed to a remote compaction service off the compute nodes.

Because the live state files *already sit* in the same DFS as checkpoints, two things fall out:

- **Checkpointing gets lightweight.** A checkpoint mostly references files instead of uploading them (the coordination is still the classic [Chandy–Lamport-style snapshot](/articles/distributed-systems/2026-07-26-chandy-lamport-snapshots)). The paper reports stable checkpoints completing **within 3 seconds** on a workload where Flink 1.20's incremental checkpoints took up to 60.
- **Recovery and rescaling stop moving data.** A restarted or rescaled task reads state lazily from DFS and warms its cache in the background: the paper measures roughly **16× faster recovery, 49× faster scale-out, 12× faster scale-in** versus RocksDB. That makes autoscaling stateful jobs on spot instances an operational reality instead of a slide.

## Async state access: hiding the 10× latency

Remote state access is ~10× slower per operation than local disk, which would sink throughput if operators still did blocking `state.get()` calls. So 2.0 pairs ForSt with a new **asynchronous execution model**: an Async Execution Controller interleaves state I/O for *different keys* while preserving per-key record order and watermark semantics — overlapping CPU with remote I/O instead of stalling on it. On Nexmark's I/O-heavy queries, async disaggregated state with a local cache matches or beats the local-RocksDB setup ([Alibaba's write-up](https://www.alibabacloud.com/blog/apache-flink-2-0-streaming-into-the-future_602008) reports up to 50% faster with a warm cache); the honest flip side from the same benchmarks: small-state jobs (tens to hundreds of MB) see ~10% overhead — if your state fits in RAM, disaggregation buys you nothing but ops flexibility.

For SQL you flip a flag; for DataStream you must use the new **State V2** async APIs (`StateFuture`-returning calls) — old synchronous `ValueState` code doesn't become async by magic.

## Quickstart config (Flink 2.3)

```yaml
# config.yaml  (flink-conf.yaml is gone in 2.x)
state.backend.type: forst
execution.checkpointing.incremental: true
execution.checkpointing.dir: s3://my-bucket/flink-checkpoints
# optional: separate primary state location + local cache sizing
state.backend.forst.primary-dir: s3://my-bucket/forst-state
state.backend.forst.cache.dir: /mnt/nvme/forst-cache
state.backend.forst.cache.size-based-limit: 20GB

# SQL jobs: enable async state operators
table.exec.async-state.enabled: true
table.exec.mini-batch.enabled: false          # not yet supported with async state
table.optimizer.agg-phase-strategy: ONE_PHASE # two-phase agg not yet supported
```

## The rest of the 2.x story

The major-version break also **removed the legacy APIs**: the DataSet API, the Scala DataStream API, `SourceFunction`/`SinkFunction`, and the legacy `TableSource`/`TableSink` are gone — migration targets are DataStream + Source/Sink V2 (and Table API/SQL for [batch-on-stream](/articles/sys-patterns/2026-07-30-event-based-batch-processing) workloads). Checkpointing kept improving after 2.0: [2.3.0](https://flink.apache.org/2026/06/25/apache-flink-2.3.0-release-announcement/) can trigger checkpoints *while a job is still recovering* from unaligned checkpoints (`execution.checkpointing.unaligned.during-recovery.enabled`), so a restart loop no longer loses all in-flight progress, and redesigned watermark-alignment buffering speeds up backlog catch-up.

## Maturity, honestly

The docs still label disaggregated state and async execution as evolving — it is explicitly **encouraged for large state**, not a default: mini-batch and two-phase aggregation don't work with async state yet, only a subset of SQL operators (joins, ranks, window aggregates, deduplication…) have async implementations, DataStream users must port to State V2, and a next-generation Rust-based ForSt core was targeted for mid-2026. Production migration reports ([one practitioner's 2026 review](https://medium.com/@fiorello.matteo/apache-flink-in-2026-a-production-users-deep-dive-into-what-s-new-ff19d265490f)) say the async-state and SQL gains justify the 1.x→2.x jump, but the removed APIs make it a real port, not a version bump. The interview-ready summary: *disaggregated state trades per-access latency (hidden by async execution + cache) for elastic ownership of state — which is exactly the trade cloud deployments want, because rescaling, recovery, and checkpointing all become metadata operations instead of data movement.*

**Try next:** Take a Flink SQL job with >1 GB of keyed state, run it once on RocksDB and once with the ForSt config above against MinIO, and compare checkpoint duration and time-to-first-record after a `kill` — the delta is the whole argument.
