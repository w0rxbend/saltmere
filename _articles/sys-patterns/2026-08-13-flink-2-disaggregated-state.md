---
title: "Flink 2.x disaggregated state: ForSt places keyed state on object storage"
date: 2026-08-13
track: sys-patterns
summary: "Flink 2.x (2.3.0 current as of mid-2026) inverts the state model: the ForSt backend makes object storage the primary home of keyed state, with local disk demoted to cache, and an asynchronous execution model overlaps the remote latency with computation. The reported effects on rescaling, checkpoint duration and provisioning, and the edges still marked evolving."
reading_time: 6
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

**Gist.** For a decade, large keyed state in Apache Flink meant RocksDB on local disk, which binds state ownership to a machine: rescaling redistributes terabytes, recovery downloads state before the first record flows, and checkpointing uploads deltas on the hot path. Flink 2.x's **ForSt** state backend inverts the hierarchy — a distributed filesystem or object store (S3, OSS, HDFS) becomes the primary home of state and local disk becomes a cache — so rescale, recovery and checkpoint become largely metadata operations. The cost is that every state access that misses the cache crosses a network: the VLDB paper's latency table puts a remote read two orders of magnitude above a local one (68 µs on NVMe, 23 ms on object storage), which forces a new asynchronous execution model and a corresponding rewrite of user state code.

## The disk-bound provisioning problem

In the container model, a task manager's state size dictates its pod size, so compute is provisioned to obtain storage rather than CPU. The [VLDB 2025 paper](https://www.vldb.org/pvldb/vol18/p4846-mei.pdf) by Mei et al. reports that **35% of the several hundred jobs surveyed in Alibaba's logistics business were disk-bound** — taking extra compute units only to obtain storage. The threshold used to classify a job is 20 GB of state per CPU core, one Alibaba Cloud compute unit.

The release line: 2.0 (March 2025) introduced disaggregated state; 2.1 (July 2025) and 2.2 (December 2025) iterated; **2.3.0 (June 2026) is the current stable release**.

## ForSt: the LSM tree addresses a distributed filesystem

ForSt ("For Streaming") is a RocksDB descendant that reads and writes sorted-string-table (SST) files of its [log-structured merge (LSM) tree](/articles/distributed-systems/2026-08-10-lsm-trees-vs-b-trees) on a distributed filesystem directly, rather than on a local volume. Local disk, configured through `state.backend.forst.cache.dir`, holds a tiered cache of hot files, so **the working set is served locally while ownership of the files remains remote**. Compaction, which is CPU-heavy when co-located, can be delegated to stateless compactor workers off the compute nodes; the paper describes remote compaction as an experimental feature.

The invariant that makes the rest work is placement: **the live state files already reside in the same distributed filesystem that holds checkpoints**. Two consequences follow.

- **Checkpointing degenerates to referencing.** A checkpoint mostly records references to files already present in the store instead of uploading their contents. The coordination protocol is unchanged — the classic [Chandy–Lamport-style barrier snapshot](/articles/distributed-systems/2026-07-26-chandy-lamport-snapshots) still establishes the consistent cut. Over 300 checkpoints at a one-minute interval, the paper reports **every Flink 2.0 checkpoint finishing within 3 seconds** regardless of size, while Flink 1.20's incremental checkpoints — averaging 1.89 GB — exceeded 30 seconds in 19.7% of cases and 50 seconds in more than 1.5%.
- **Recovery and rescaling stop moving data.** A restarted or rescaled task reads state lazily from the distributed filesystem and warms its cache in the background rather than downloading a full state handle before processing. The paper measures roughly **16× faster recovery, 49× faster scale-out and 12× faster scale-in** against RocksDB.

## Asynchronous execution: overlapping the remote round trip

The paper's measured read latencies are 68 µs for NVMe and 199 µs for an ESSD PL1 volume against 1.5 ms for HDFS and 23 ms for object storage: **remote reads are two orders of magnitude slower** than local ones. Under the pre-2.0 execution model an operator processes one record at a time and blocks on each synchronous state read, so the task thread is idle for the duration of every round trip. [Alibaba's write-up](https://www.alibabacloud.com/blog/apache-flink-2-0-streaming-into-the-future_602008) puts the resulting cost at **around 10× slower** on a streaming-aggregation benchmark when a disaggregated store is accessed synchronously.

Flink 2.0 therefore pairs ForSt with an **Async Execution Controller**. State requests are issued without blocking the task thread, and the controller interleaves in-flight state input/output for **different keys** while preserving **per-key record order and watermark semantics**. Records for the same key remain serialised; records for distinct keys proceed concurrently, so CPU work overlaps with outstanding network requests instead of waiting behind them. Checkpoint barriers must observe a quiescent point, which is why the controller must drain in-flight requests before a barrier is emitted.

On the I/O-heavy queries of the Nexmark benchmark (state sizes of 1.2 GB to 4.8 GB), the 2.0 release announcement reports asynchronous disaggregated state with a 1 GB cache reaching **75% to 120% of the throughput** of the local state store; the paper puts the same configuration 4% ahead on average. Without any cache the asynchronous model retains roughly half the local throughput. The benchmarks record the opposite result for small state: queries holding 10 MB to 400 MB, where the block cache absorbs the working set, trail the local configuration by **no more than 10% on average**. Where state fits in memory, disaggregation yields operational elasticity and nothing else.

The migration consequence is concrete. SQL jobs enable asynchronous operators through configuration. DataStream jobs must be ported to the **State V2** APIs, whose accessors return a `StateFuture` rather than a value; existing synchronous `ValueState` code continues to execute synchronously and obtains none of the overlap.

### Implementation sketch (Scala)

The load-bearing shape of a State V2 operator: the handler returns immediately after registering a continuation, so the task thread is free to admit a record for a different key.

The Scala DataStream API is removed in 2.x, so this calls the Java one from Scala 3.

```scala
import org.apache.flink.api.common.state.v2.{ValueState, ValueStateDescriptor}
import scala.compiletime.uninitialized

class RunningTotal extends KeyedProcessFunction[String, Order, Total]:

  // java.lang.Long, not scala.Long: the state value is nullable when unset.
  private var sum: ValueState[java.lang.Long] = uninitialized

  override def open(params: OpenContext): Unit =
    sum = getRuntimeContext.getState(
      ValueStateDescriptor[java.lang.Long]("sum", Types.LONG))

  override def processElement(
      order: Order,
      ctx: KeyedProcessFunction[String, Order, Total]#Context,
      out: Collector[Total]): Unit =
    // asyncValue() returns a StateFuture; no round trip is awaited here.
    sum.asyncValue().thenCompose: current =>
      val next = (if current == null then 0L else current.longValue) + order.amount
      // The continuation runs on the task thread, so out.collect is safe.
      out.collect(Total(order.key, next))
      sum.asyncUpdate(next)
    ()
```

The synchronous rewrite of the same logic — `val current = sum.value()` — is a single-line change and costs the entire overlap: the thread parks on each read.

## Configuration (Flink 2.3)

```yaml
# config.yaml  (flink-conf.yaml is removed in 2.x)
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

## The remainder of the 2.x break

The major version also **removed the legacy APIs**: the DataSet API, the Scala DataStream API, `SourceFunction`/`SinkFunction`, and the legacy `TableSource`/`TableSink`. Migration targets are DataStream with Source/Sink V2, and Table API/SQL for [batch-on-stream](/articles/sys-patterns/2026-07-30-event-based-batch-processing) workloads.

Checkpointing continued to change after 2.0. [Release 2.3.0](https://flink.apache.org/2026/06/25/apache-flink-2.3.0-release-announcement/) can trigger checkpoints **while a job is still recovering** from unaligned checkpoints, under `execution.checkpointing.unaligned.during-recovery.enabled`, paired with `execution.checkpointing.unaligned.recover-output-on-downstream.enabled`. The same release redesigns watermark-alignment buffering to speed up backlog catch-up.

## Maturity

The documentation scopes disaggregated state to **extremely large state** and states plainly that when state is small, local state with synchronous access is the better choice. The stated limitations: mini-batch and two-phase aggregation do not work with asynchronous state; only a subset of SQL operators has asynchronous implementations (rank, row-time deduplication, non-distinct aggregate, join, window join, and tumbling, hopping and cumulative window aggregates); and DataStream users must port to State V2.

The trade in one sentence: **disaggregated state exchanges per-access latency — masked by asynchronous execution and the local cache — for elastic ownership of state, converting rescaling, recovery and checkpointing from data movement into metadata operations.**

## Pitfalls

- **A ported job shows no throughput gain because state access is still synchronous.** Calling `value()` instead of `asyncValue()` compiles and runs correctly; the Async Execution Controller has nothing to interleave and each access pays the full remote round trip.
- **Enabling `table.exec.async-state.enabled` alongside mini-batch or two-phase aggregation.** Both are documented as unsupported with asynchronous state, which is why the configuration above disables mini-batch and pins `agg-phase-strategy` to `ONE_PHASE`.
- **A small-state job regresses after migration.** Benchmarks report up to 10% lower throughput for state in the 10 MB to 400 MB range, where the cache absorbs everything and the asynchronous machinery adds cost without hiding any latency.
- **A cold cache after rescaling makes the first minutes slow.** Recovery is fast because state is read lazily, but the working set must be re-warmed from the distributed filesystem; time-to-first-record improves while steady-state throughput arrives later.
- **Undersized `state.backend.forst.cache.size-based-limit` turns hot reads into network reads.** The working set no longer fits the tiered cache, so the two-orders-of-magnitude per-read penalty applies to traffic that previously stayed local. The documented default for that limit is 1 GB.
- **Treating 1.x→2.x as a version bump.** The DataSet API, the Scala DataStream API and `SourceFunction`/`SinkFunction` are removed, so jobs using them do not compile against 2.x at all.
