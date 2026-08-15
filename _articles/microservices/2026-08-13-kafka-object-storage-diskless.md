---
title: "Diskless Kafka: WarpStream, AutoMQ, Freight, and KIP-1150"
date: 2026-08-13
track: microservices
summary: "Cross-availability-zone replication traffic, not compute or storage, dominates cloud Kafka bills — Aiven reports it at over 80% of spend. The diskless designs write topic data straight to object storage instead: WarpStream (now Confluent), AutoMQ, Confluent Freight, and KIP-1150 Diskless Topics, accepted into Apache Kafka in March 2026. The price is produce latency in the hundreds of milliseconds to seconds."
reading_time: 6
tags: [kafka, object-storage, diskless, warpstream, kip-1150]
sources:
  - title: "KIP-1150: Diskless Topics (Apache Kafka wiki)"
    url: "https://cwiki.apache.org/confluence/display/KAFKA/KIP-1150%3A+Diskless+Topics"
  - title: "Aiven — KIP-1150 Accepted, and the Road Ahead"
    url: "https://aiven.io/blog/kip-1150-accepted-and-the-road-ahead"
  - title: "WarpStream — Kafka is dead, long live Kafka"
    url: "https://www.warpstream.com/blog/kafka-is-dead-long-live-kafka"
  - title: "Confluent — Freight clusters are Generally Available"
    url: "https://www.confluent.io/blog/freight-clusters-are-generally-available/"
  - title: "AutoMQ — Diskless Kafka on S3 (GitHub)"
    url: "https://github.com/AutoMQ/automq"
---

**Gist.** Classic Apache Kafka replicates every partition to three brokers spread across availability zones (AZs), and cloud providers meter every byte that crosses an AZ boundary, so the network line dominates the bill. Diskless designs remove replication from the write path entirely: brokers buffer batches and flush them to object storage, whose own multi-AZ durability replaces broker-to-broker copying, while ordering moves from the partition leader to a metadata coordinator. The cost is latency — a single object-storage PUT plus the batching interval needed to keep API call counts bounded pushes produce latency from sub-100 ms into the hundreds of milliseconds to seconds.

## The cost argument, with numbers

AWS charges about $0.01/GiB out plus $0.01/GiB in for cross-AZ traffic, so roughly **$0.02 per zone crossing**; the KIP-1150 text cites $0.02/GiB on AWS and $0.01 on GCP. With replication factor 3 and producers spread across zones, about two-thirds of produce traffic crosses a zone before replication starts, and replication then crosses two more times. WarpStream's arithmetic follows directly: `$0.02 × 2/3 + $0.02 × 2 ≈ $0.053` per GiB **written**, before storage or consumption is counted.

WarpStream's worked example puts a sustained high-throughput produce workload with several consumer groups at **hundreds of dollars per day in inter-zone networking alone** on self-hosted Kafka, against a small fraction of that in interzone traffic plus a comparably small S3 API bill on its object-storage architecture — a reduction of roughly an order of magnitude on the networking line. Aiven reports the same shape independently: ">80% of a cloud Kafka bill tied to cross-zone traffic alone," with KIP-1150 pitched as cutting total cost of ownership (TCO) by up to 80%. Confluent claims up to 90% lower infrastructure cost for Freight against self-managed Kafka. The three figures are not comparable measurements, but they identify the same dominant term.

The counterweight is latency. An object-storage PUT takes tens to hundreds of milliseconds, and batching to keep API request cost bounded adds the batch interval on top. **WarpStream cites ~400 ms p99 produce latency and ~1 s end-to-end.** Freight quotes "a second or two" against sub-100 ms on standard clusters. Aiven reports p99 in the **low seconds** on early production logging workloads, with further reduction described as ongoing work. The applicable workloads are therefore throughput-heavy and latency-tolerant — logs, telemetry, change-data-capture backfill, feature pipelines — not request paths with tens-of-milliseconds budgets.

## The implementations, as of August 2026

| | What it is | Status | Latency posture |
|---|---|---|---|
| WarpStream | Kafka-protocol-compatible rewrite; stateless agents, all data in S3 | Acquired by Confluent (Sept 2024); sold as BYOC | ~400 ms p99 produce |
| Confluent Freight | Confluent Cloud cluster type writing directly to object storage | Generally available on AWS | ~1–2 s |
| AutoMQ | Kafka fork: broker code kept, storage layer swapped for S3 | Open source on GitHub + commercial cloud | Single-digit ms (EBS WAL) or S3-level (S3 WAL) |
| KIP-1150 Diskless Topics | Diskless as a *topic type* in Apache Kafka proper | Accepted March 2026; sub-KIPs in development, not yet released | Target: seconds-class, per-topic opt-in |

**WarpStream** appeared in 2023. Its agents hold no local disks, data and metadata are separated, and offsets are assigned by a metadata service rather than by partition leaders. Because any agent in any zone can serve any partition, a zone-local client never pays cross-AZ rates. Confluent acquired the product in September 2024.

**AutoMQ** keeps Apache Kafka's compute layer and replaces only the storage engine with a stream abstraction over S3. Its write-ahead log (WAL) is pluggable: an Elastic Block Store (EBS) or regional-disk WAL holds produce latency in single-digit milliseconds while the data still lands in S3, whereas a pure S3 WAL is fully diskless. A pluggable WAL means the category's latency floor is not fixed at the object-storage round trip.

**KIP-1150** brings the model upstream. Aiven proposed it in April 2025, prototyped it in a fork named *Inkless*, and the KIP was **accepted in March 2026**. Diskless topics are specified to coexist with classic topics in one cluster, making the choice between low-latency-and-expensive and seconds-and-cheap a per-topic decision. The work is split into sub-KIPs, chiefly **KIP-1163 (Diskless Core)** and **KIP-1164 (Diskless Coordinator)**, both still in development. No shipped Kafka release contains diskless topics; the accurate description is "accepted, being implemented," not "available."

## Leaderless data path, coordinated ordering

The structural change is the removal of the partition leader from the write path. Classic Kafka funnels a partition's writes through one leader broker, which is simultaneously the source of the replication traffic and of partition-level hot spots. In diskless designs the data path is **leaderless**: any broker or agent in any zone accepts produce requests for any partition, buffers batches drawn from many partitions together, and flushes them as **combined objects** to object storage. One object therefore contains batches belonging to several partitions and several producers, which is what keeps the object count — and the API bill — bounded at high fan-in.

Ordering, previously an artefact of there being exactly one leader, becomes an explicit step. A **coordinator** assigns offsets at commit time: WarpStream's metadata service, and in KIP-1150 the batch coordinator specified by KIP-1164. The invariant is that a batch is visible to consumers only after the coordinator has recorded both its offset range and the object and position where its bytes live; the write to object storage happens first, the metadata commit second. Sequencing is thus a small-state-machine operation on metadata while the bytes bypass the brokers' replication entirely. The same split makes brokers disposable: adding or losing a broker triggers no partition re-replication, because brokers no longer own data.

This is distinct from [tiered storage (KIP-405)](/articles/microservices/2026-07-31-kafka-tiered-storage-kip405/). Tiering offloads cold, closed segments to object storage, but the hot path still runs through leaders, local disks and cross-AZ replication, so the dominant cost term survives. Diskless moves the write path itself to object storage.

### Implementation sketch (Scala)

The load-bearing idea is the commit-time offset assignment: bytes land in the object first, then a single-threaded coordinator step turns a set of per-partition batch descriptors into offset ranges.

```scala
final case class TopicPartition(topic: String, partition: Int)

/** Where a batch's bytes live inside a combined object. */
final case class BatchRef(objectKey: String, offsetInObject: Long, byteLen: Int, records: Int)

final case class Committed(tp: TopicPartition, base: Long, ref: BatchRef)

/** Sequencing is metadata-only: the object is already durable when this runs. */
final class BatchCoordinator:
  private val nextOffset = scala.collection.mutable.Map.empty[TopicPartition, Long]

  /** Assigns contiguous offset ranges for one flushed object, atomically per call. */
  def commit(batches: Seq[(TopicPartition, BatchRef)]): Seq[Committed] = synchronized:
    batches.map: (tp, ref) =>
      val base = nextOffset.getOrElse(tp, 0L)
      nextOffset(tp) = base + ref.records
      Committed(tp, base, ref)

// Producer-side path on any broker, in any zone: buffer across partitions, flush once.
def flush(buffer: Seq[(TopicPartition, Array[Byte])], coord: BatchCoordinator): Seq[Committed] =
  val key = s"diskless/${java.util.UUID.randomUUID()}"
  var pos = 0L
  val refs = buffer.map: (tp, bytes) =>
    val ref = BatchRef(key, pos, bytes.length, records = 1)
    pos += bytes.length
    (tp, ref)
  // putObject(key, concatenated bytes) must complete before commit; a crash here
  // leaves an orphan object, never a visible record with no bytes behind it.
  coord.commit(refs)
```

## Pitfalls

- **Treating diskless as a drop-in replacement for every topic.** Produce latency moves from sub-100 ms to ~400 ms p99 (WarpStream) or seconds (Freight, Aiven's early measurements); a request path with a tens-of-milliseconds budget breaks under it.
- **Assuming KIP-1150 is usable today.** It was accepted in March 2026, but KIP-1163 and KIP-1164 are in development and no Kafka release ships diskless topics; a design that depends on it depends on unreleased code.
- **Conflating tiered storage with diskless.** KIP-405 tiering moves closed segments to object storage while leaders, local disks and cross-AZ replication still carry the hot path, so the cross-AZ line on the bill does not move.
- **Reducing the batching interval to recover latency.** Object-storage cost is per request as well as per byte; smaller batches mean more PUTs, and WarpStream's low API-cost figure assumes batching large enough to keep request counts down.
- **Extrapolating the vendor savings figures.** The 80% (Aiven/KIP-1150) and 90% (Confluent Freight) claims come from different workloads and different baselines, and none is a controlled comparison against the others.
- **Ignoring the crash window between object write and metadata commit.** The bytes are durable before the coordinator assigns offsets, so a failure in between leaves an object no partition references — storage that reclamation must handle, not lost data.
