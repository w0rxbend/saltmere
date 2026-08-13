---
title: "Diskless Kafka: WarpStream, AutoMQ, Freight, and KIP-1150"
date: 2026-08-13
track: microservices
summary: "Cross-AZ replication traffic, not compute or storage, dominates cloud Kafka bills — Aiven sees it at over 80% of spend. The diskless movement writes topics straight to S3 instead: WarpStream (now Confluent), AutoMQ, Confluent Freight, and KIP-1150 Diskless Topics, accepted into Apache Kafka in March 2026. The price is latency in the hundreds of milliseconds to seconds."
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

The biggest architectural shift in the Kafka ecosystem right now is not a feature — it's a bill. Classic Kafka replicates every partition to three brokers spread across availability zones, and cloud providers charge for every byte that crosses a zone boundary. The "diskless" movement's answer: stop replicating, write batches directly to object storage, and let S3's own three-AZ durability do the job. In March 2026 that idea landed in Apache Kafka itself as the accepted KIP-1150.

## The cost argument, with numbers

AWS charges about $0.01/GiB out plus $0.01/GiB in for cross-AZ traffic — ~$0.02 per crossing (the KIP-1150 text cites $0.02/GiB on AWS, $0.01 on GCP). With replication factor 3 and zone-spread clients, roughly two-thirds of produce traffic crosses a zone before replication even starts, then replication crosses two more times. WarpStream's arithmetic: `$0.02 × 2/3 + $0.02 × 2 ≈ $0.053` per GiB *written* — before you've stored or consumed anything.

WarpStream's worked example: a 140 MiB/s produce workload with three consumers costs self-hosted Kafka about **$641/day in inter-zone networking alone** (~$234k/year). The same workload on their S3-direct architecture: **under $15/day** of interzone traffic plus **under $40/day** of S3 API calls. Aiven reports the same shape from the other side: "we routinely see >80% of a cloud Kafka bill tied to cross-zone traffic alone," and KIP-1150's pitch is cutting Kafka TCO by up to 80%. Confluent claims up to 90% lower infrastructure cost for Freight versus self-managed Kafka. The numbers differ; the diagnosis is identical.

The trade is latency. An S3 PUT takes tens to hundreds of milliseconds, and batching for sane API costs adds more: WarpStream cites **~400 ms p99 produce latency** and ~1 s end-to-end; Freight quotes "a second or two" versus sub-100 ms on standard clusters; Aiven measured p99 around 3–3.5 s on early production logging workloads, with a path to sub-2 s. Diskless is for throughput-heavy, latency-tolerant streams — logs, telemetry, CDC backfill, ML feature pipelines — not for your 50 ms trading path.

## The players, verified as of August 2026

| | What it is | Status | Latency posture |
|---|---|---|---|
| WarpStream | Kafka-protocol-compatible rewrite; stateless agents, all data in S3 | Acquired by Confluent (Sept 2024); sold as BYOC | ~400 ms p99 produce |
| Confluent Freight | Confluent Cloud cluster type writing directly to object storage | GA since Feb 2025 on AWS | ~1–2 s |
| AutoMQ | Kafka fork: broker code kept, storage layer swapped for S3 | Open source on GitHub + commercial cloud | Single-digit ms (EBS WAL) or S3-level (S3 WAL) |
| KIP-1150 Diskless Topics | Diskless as a *topic type* in Apache Kafka proper | Accepted 2 Mar 2026; sub-KIPs in development, not yet released | Target: seconds-class, per-topic opt-in |

**WarpStream** started the movement in 2023: agents are completely stateless (no local disks at all), data and metadata are separated, and offsets are assigned by a metadata service rather than partition leaders. Because any agent in any zone can serve any partition, zone-local clients never pay cross-AZ rates. Confluent acquired it in September 2024.

**AutoMQ** takes a different fork: keep Apache Kafka's compute layer byte-for-byte and replace only the storage engine with a stream abstraction over S3. Its WAL is pluggable — an EBS or regional-disk WAL keeps produce latency in single-digit milliseconds while data still lands in S3, or a pure S3 WAL goes fully diskless. That makes it the "you don't have to accept 400 ms" counterargument in the category.

**KIP-1150** is the upstream endgame. Proposed by Aiven in April 2025 (prototyped in their deliberately short-lived *Inkless* fork), it was accepted on 2 March 2026 with 9 binding votes. Diskless topics will coexist with classic topics in the same cluster — you choose per topic between milliseconds-and-expensive and seconds-and-cheap. The work is split into sub-KIPs, chiefly **KIP-1163 (Diskless Core)** and **KIP-1164 (Diskless Coordinator)**, both still in development; no shipped Kafka release includes diskless topics yet, so in an interview say "accepted, being implemented," not "available."

## Leaderless by design

The interesting architecture shift is what happens to the partition leader. Classic Kafka funnels every partition's writes through one leader broker — that's what creates both the replication traffic and the hot-spot. In diskless designs the data path is **leaderless**: any broker/agent in any zone accepts produces for any partition, buffers batches from many partitions, and flushes them as combined objects to S3. Ordering, which the leader used to provide, moves to a **coordinator** that assigns offsets at commit time (WarpStream's metadata service; KIP-1150's batch coordinator per KIP-1164). Sequencing becomes a metadata operation on a small state machine, while the bytes go straight to object storage. That's also what makes brokers disposable: scaling or failure means no partition re-replication, because brokers no longer own data.

Don't confuse this with [tiered storage (KIP-405)](/articles/microservices/2026-07-31-kafka-tiered-storage-kip405/), which every diskless vendor is quick to point out: tiering offloads *cold, closed* segments to S3 but the hot path still runs through leaders, local disks, and cross-AZ replication — the dominant cost survives. Diskless moves the *write path itself* to object storage. It's the difference between archiving to S3 and living on it.

The interview framing: this is the same separation-of-storage-and-compute wave that produced Snowflake and Neon, arriving at streaming — and the fact that Kafka (KRaft-era, [post-ZooKeeper](/articles/microservices/2026-07-25-kafka-4-kraft-no-zookeeper/)) accepted KIP-1150 rather than ceding the ground to proprietary rewrites is the strongest signal the model won.

**Try next:** pull your Kafka cluster's inter-AZ transfer line out of last month's cloud bill and compute its share of total streaming spend. If it's over half — and your consumers tolerate seconds of latency — price the same workload on Freight or AutoMQ, and note which topics could opt into diskless once KIP-1150 ships.
