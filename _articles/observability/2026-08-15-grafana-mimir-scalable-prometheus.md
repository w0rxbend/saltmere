---
title: "Grafana Mimir: When One Prometheus Is Not Enough"
date: 2026-08-15
track: observability
summary: "Mimir turns Prometheus remote_write fan-in into a horizontally scalable, multi-tenant metrics backend: distributors, ingesters, queriers, store-gateways, and a split-and-merge compactor demonstrated at 1 billion active series. The architecture, a one-container monolithic setup, and the conditions under which Thanos or VictoriaMetrics is the better choice."
reading_time: 6
tags: [mimir, prometheus, remote-write, thanos, victoriametrics, tsdb, grafana]
sources:
  - title: "Grafana Mimir — Get started (monolithic mode)"
    url: "https://grafana.com/docs/mimir/latest/get-started/"
  - title: "Grafana Mimir docs — Compactor and split-and-merge compaction"
    url: "https://grafana.com/docs/mimir/latest/references/architecture/components/compactor/"
  - title: "Grafana Labs — How we scaled our new Prometheus TSDB Grafana Mimir to 1 billion active series"
    url: "https://grafana.com/blog/2022/04/08/how-we-scaled-our-new-prometheus-tsdb-grafana-mimir-to-1-billion-active-series/"
  - title: "Grafana Labs — Grafana Mimir 3.0 release"
    url: "https://grafana.com/blog/grafana-mimir-3-0-release-all-the-latest-updates/"
  - title: "Greptime — Prometheus Long-Term Storage in 2026: The Options Compared (independent)"
    url: "https://greptime.com/tech-content/2026-06-17-prometheus-long-term-storage-options"
---

**Gist.** A single Prometheus process is one binary with one local disk and no tenancy boundary, so a fleet of twenty clusters yields twenty disjoint query surfaces and twenty retention policies. Grafana Mimir is a `remote_write` fan-in target — a 2022 fork of Cortex, at major version **3.x** — that accepts writes from every Prometheus, replicates them across ingesters, persists blocks to object storage, and answers PromQL across the whole set. The cost is component count: rings, replication factors, a sharded compactor, and — where the 3.x Kafka-backed write path is chosen — a Kafka cluster, all of which must be operated.

## The write path and the read path

Mimir is a set of microservices arranged around an object store (Amazon Simple Storage Service (S3), Google Cloud Storage (GCS) or Azure Blob Storage (ABS)). On the **write path** a `remote_write` request reaches a **distributor**, which validates samples, enforces per-tenant limits, and shards each series by hash across **ingesters** at a **default replication factor of 3**. An ingester holds roughly the **last two hours** of samples in a time series database (TSDB) head block, then compacts and ships that block to object storage. Since Mimir 3.0 the distributor can instead append to **Kafka-based "ingest storage"**, from which ingesters consume; the documented effect is that reads and writes are decoupled, so query load on ingesters does not stall ingestion.

On the **read path**, a **query-frontend** splits queries by time range and caches results, and **queriers** execute PromQL. Version 3.0 made the streaming **Mimir Query Engine (MQE)** the default; Grafana Labs reports **substantially lower peak query memory** relative to the upstream Prometheus engine, the streaming execution model being the stated cause; no independent benchmark quantifies the reduction. A querier reads recent samples from ingesters and historical samples through **store-gateways**, which maintain indexes over the blocks in object storage. The **compactor** operates asynchronously, merging small blocks into larger ones. Optional **ruler** and **alertmanager** components evaluate recording and alerting rules and route notifications per tenant.

Multi-tenancy is not layered on top: every request carries an `X-Scope-OrgID` header, and limits, retention and compaction sharding are all keyed by that tenant identifier.

## Split-and-merge compaction

Prometheus TSDB compaction produces **one output block per time range**. That assumption fails at scale for a concrete format reason: the **TSDB index format has a hard size limit of 64 GB**, so a sufficiently large tenant cannot be represented as a single compacted block.

Mimir's **split-and-merge compactor** replaces the single output with a two-stage plan.

1. **Split.** Source blocks for a time range are partitioned into `N` groups (`-compactor.split-groups`). Each group is compacted independently into `M` shard-blocks, where each shard-block holds a **disjoint subset of the series** selected by hashing the series labels (`-compactor.split-and-merge-shards`, sized at roughly **one shard per 8 million active series**).
2. **Merge.** The resulting `N × M` intermediate blocks are merged shard-wise into **`M` final blocks**, one per shard.

The invariant that makes this parallel is that **shard membership is a pure function of the series labels**, identical in every group. Two intermediate blocks carrying the same shard identifier therefore contain series drawn from the same hash range and from no other, so merging them requires no cross-shard coordination and every job — each split, each merge — is independent and schedulable on a separate machine.

This is the mechanism underneath the 1-billion-active-series load test reported by Grafana Labs: **a single tenant of 1 billion active series**, which at the default replication factor of 3 is 3 billion series held across ingesters, with compaction spread over **a horizontally scaled compactor fleet** so that queries were not served from a sprawl of uncompacted blocks. The published figures are Grafana Labs' own load test, not an independent measurement.

### Implementation sketch (Scala)

The load-bearing idea is the shard function and the shape of the job graph it induces, not the block format.

```scala
final case class SeriesId(labels: String)
final case class BlockRef(group: Int, shard: Int)

/** Shard membership depends only on the labels, so every split job in every
  * group assigns a given series to the same shard. */
def shardOf(s: SeriesId, shards: Int): Int =
  math.floorMod(scala.util.hashing.MurmurHash3.stringHash(s.labels), shards)

def split(group: Int, sources: Vector[SeriesId], shards: Int): Map[BlockRef, Vector[SeriesId]] =
  sources.groupBy(s => BlockRef(group, shardOf(s, shards)))

def plan(groups: Int, shards: Int, sources: Vector[Vector[SeriesId]])
    : Map[Int, Vector[Vector[SeriesId]]] =
  val intermediates: Map[BlockRef, Vector[SeriesId]] =
    sources.zipWithIndex.flatMap((src, g) => split(g, src, shards)).toMap
  // Merge jobs are keyed by shard: the N intermediates sharing a shard id
  // hold disjoint-from-other-shards series, so they merge without coordination.
  intermediates.toVector
    .groupBy(_._1.shard)
    .view.mapValues(_.map(_._2))
    .toMap

// Each entry of `plan(...)` is one independently schedulable merge job,
// producing one of the M final blocks for the time range.
```

## Monolithic mode

The core components run inside a single process under the default `-target=all`, which suits development and small production deployments; several replicas of that monolith can run for availability. Microservices mode, where each process receives a single `-target=ingester`, `-target=distributor` and so on, is what permits ingest and query capacity to be scaled independently.

```bash
docker run --rm --name mimir -p 9009:9009 \
  -v "$(pwd)"/demo.yaml:/etc/mimir/demo.yaml \
  grafana/mimir:latest --config.file=/etc/mimir/demo.yaml
```

A minimal `demo.yaml` — filesystem backend instead of S3, multi-tenancy off, replication factor 1:

```yaml
multitenancy_enabled: false
server:
  http_listen_port: 9009
blocks_storage:
  backend: filesystem
  filesystem: { dir: /tmp/mimir/data/tsdb }
  bucket_store: { sync_dir: /tmp/mimir/tsdb-sync }
ingester:
  ring:
    instance_addr: 127.0.0.1
    kvstore: { store: memberlist }
    replication_factor: 1
store_gateway:
  sharding_ring: { replication_factor: 1 }
compactor:
  data_dir: /tmp/mimir/compactor
ruler_storage:
  backend: filesystem
  filesystem: { dir: /tmp/mimir/rules }
```

Any Prometheus, or Grafana Alloy, then writes to it. With multi-tenancy enabled the tenant header is required:

```yaml
# prometheus.yml
remote_write:
  - url: http://localhost:9009/api/v1/push
    # headers: { X-Scope-OrgID: team-payments }  # when multitenancy_enabled: true
```

Grafana reads it as an ordinary Prometheus data source at `http://localhost:9009/prometheus`.

## When Mimir is the wrong instrument

Where an existing Prometheus fleet works and the requirement is retention plus a single query view, a **Thanos sidecar** beside each Prometheus is the smaller change: Prometheus remains the source of truth and uploads its own blocks. Where the pressure is cardinality and operational headcount rather than tenancy, [VictoriaMetrics](/articles/observability/2026-08-14-victoriametrics-victorialogs) presents the same problem as one binary.

| | Prometheus | Thanos | Mimir | VictoriaMetrics |
|---|---|---|---|---|
| Model | single process | sidecars + query layer over existing Proms | remote_write fan-in, microservices | remote_write fan-in, single binary or 3-service cluster |
| Long-term storage | local disk | object storage | object storage (optional Kafka buffer in 3.x) | local disk (single/cluster) |
| Multi-tenancy | no | basic (labels) | first-class, per-tenant limits | via cluster version |
| Ops burden | trivial | moderate (per-Prom sidecars, compactor, store) | high (many services, rings, optional Kafka in 3.x) | low |
| Query language | PromQL | PromQL | PromQL (MQE, streaming) | PromQL + MetricsQL superset |
| License | Apache-2.0 | Apache-2.0 (CNCF) | AGPLv3 | Apache-2.0 |
| Sweet spot | one cluster/team | keep existing Proms, add retention | centralized multi-team platform, 100M+ series | efficiency-first, small ops team |

The independent comparison cited here lands on a simple partition: Thanos extends what already exists, Mimir builds a multi-tenant platform, VictoriaMetrics minimises operational surface. The property that distinguishes Mimir — absorbing a single billion-series tenant — is inseparable from its cost, which is the largest component count of the four.

## Pitfalls

- **Sizing `-compactor.split-and-merge-shards` from block count rather than series count.** Symptom: individual compaction jobs exceed the 64 GB index limit and fail repeatedly. Cause: the documented sizing is roughly one shard per 8 million active series; block count is not the constraint the index format imposes.
- **Compaction falling behind ingestion.** Symptom: query latency degrades as store-gateways fan out over many small blocks. Cause: compactor replicas are provisioned for the tenant's series count, and a fleet sized for a smaller tenant cannot keep pace once split-and-merge jobs multiply.
- **Omitting `X-Scope-OrgID` with `multitenancy_enabled: true`.** Symptom: pushes and queries are rejected. Cause: the tenant identifier is not optional; limits, retention and compaction sharding are all keyed by it.
- **Treating ingesters as stateless.** Symptom: sample loss on rolling restarts. Cause: an ingester holds roughly two hours of data in its head block before shipping to object storage; that window exists only in ingester memory and its replicas.
- **Choosing Mimir for retention alone.** Symptom: a service count disproportionate to the requirement. Cause: retention over an existing Prometheus fleet is what the Thanos sidecar model addresses, without introducing a distributor tier or hash rings.
