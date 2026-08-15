---
title: "Grafana Mimir: When One Prometheus Isn't Enough"
date: 2026-08-15
track: observability
summary: "Mimir turns Prometheus remote_write fan-in into a horizontally scalable, multi-tenant metrics backend: distributors, ingesters, queriers, store-gateways, and a split-and-merge compactor proven at 1 billion active series. Here's the architecture, a one-container monolithic setup, and an honest look at when Thanos or VictoriaMetrics is the better call."
reading_time: 5
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

A single Prometheus scales further than most people think, but it has a hard shape: one process, one disk, one tenant. The moment you have twenty clusters each running their own Prometheus and someone asks for "one dashboard across all of them, with 13 months of retention," you need a **remote_write fan-in target** — something that accepts writes from every Prometheus, deduplicates HA pairs, stores blocks in object storage, and answers PromQL over all of it. **Grafana Mimir** (a 2022 fork of Cortex, currently at **3.1.1**, June 2026) is Grafana Labs' answer, and it is built for the ugly end of that curve: multi-tenant, hundreds of millions to billions of active series.

## The write path and the read path

Mimir is a set of stateless-ish microservices around object storage (S3/GCS/ABS). On the **write path**, every `remote_write` request hits a **distributor**, which validates samples, enforces per-tenant limits, and shards series by hash across **ingesters** (replication factor 3 by default). Ingesters hold the last ~2 hours in a TSDB head block, then ship compacted blocks to object storage. Since Mimir 3.0 (November 2025), the write path can also land samples in **Kafka-based "ingest storage"** first, decoupling reads from writes so a query stampede can't stall ingestion — a real architectural shift, not a bolt-on.

On the **read path**, a **query-frontend** splits and caches queries, **queriers** execute PromQL — 3.0 made the streaming Mimir Query Engine (MQE) the default, which Grafana claims cuts peak query memory by up to 92% versus the upstream engine — fetching recent data from ingesters and historical data via **store-gateways**, which index blocks in object storage. The **compactor** runs off to the side, merging small blocks into bigger ones. Optional **ruler** and **alertmanager** components evaluate recording rules and route alerts per tenant.

Multi-tenancy is first-class: every request carries an `X-Scope-OrgID` header, and limits, retention, and even compaction sharding are per-tenant.

## Split-and-merge: why the compactor is the interesting part

Prometheus TSDB compaction assumes one output block per time range. At Mimir scale that breaks concretely: the TSDB index format caps out at 64 GB with 4 GB limits on certain index sections, so a large enough tenant simply cannot be compacted into one block. Mimir's **split-and-merge compactor** fixes this in two stages: source blocks are grouped into `N` groups (`-compactor.split-groups`), each group is compacted into `M` shard-blocks containing a subset of series (`-compactor.split-and-merge-shards`, roughly one shard per 8 million active series), and then the `N × M` intermediate blocks are merged down to `M` final blocks per shard. Every job is independent, so compaction parallelizes across machines.

This is the mechanism behind Grafana's 1-billion-active-series load test: one tenant, ~50 million samples/second ingested (3 billion series stored after replication), with ~150 compactor replicas keeping all compactions inside a 12-hour window so queries never touched uncompacted block sprawl.

## Monolithic mode: all of that, one process

You don't have to deploy a dozen Deployments to try it. With `-target=all`, every component runs in a single process — fine for dev and small production setups, and you can run several replicas of the monolith for HA. Microservices mode (each process gets `-target=ingester`, `-target=distributor`, …) is for when you need to scale ingest and query independently.

```bash
docker run --rm --name mimir -p 9009:9009 \
  -v "$(pwd)"/demo.yaml:/etc/mimir/demo.yaml \
  grafana/mimir:latest --config.file=/etc/mimir/demo.yaml
```

A minimal `demo.yaml` (filesystem instead of S3, no multi-tenancy, replication factor 1):

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

Then point any Prometheus (or Grafana Alloy) at it — with multi-tenancy on, you'd add the tenant header:

```yaml
# prometheus.yml
remote_write:
  - url: http://localhost:9009/api/v1/push
    # headers: { X-Scope-OrgID: team-payments }  # when multitenancy_enabled: true
```

Grafana queries it as a normal Prometheus data source at `http://localhost:9009/prometheus`.

## When you don't need Mimir

Honest version: most teams don't. If you have a working Prometheus fleet and mainly want retention plus one query view, a **Thanos sidecar** next to each Prometheus is far less disruptive — Prometheus stays the source of truth and uploads its own blocks. And if your pain is cardinality and operational overhead rather than multi-tenancy, [VictoriaMetrics](/articles/observability/2026-08-14-victoriametrics-victorialogs) collapses the whole problem into one binary, as we covered earlier this week.

| | Prometheus | Thanos | Mimir | VictoriaMetrics |
|---|---|---|---|---|
| Model | single process | sidecars + query layer over existing Proms | remote_write fan-in, microservices | remote_write fan-in, single binary or 3-service cluster |
| Long-term storage | local disk | object storage | object storage (Kafka buffer in 3.x) | local disk (single/cluster) |
| Multi-tenancy | no | basic (labels) | first-class, per-tenant limits | via cluster version |
| Ops burden | trivial | moderate (per-Prom sidecars, compactor, store) | high (many services, rings, Kafka in 3.x) | low |
| Query language | PromQL | PromQL | PromQL (MQE, streaming) | PromQL + MetricsQL superset |
| License | Apache-2.0 | Apache-2.0 (CNCF) | AGPLv3 | Apache-2.0 |
| Sweet spot | one cluster/team | keep your Proms, add retention | centralized multi-team platform, 100M+ series | efficiency-first, small ops team |

Rule of thumb from the independent comparisons: choose Thanos to extend what you have, Mimir to build a multi-tenant platform, VictoriaMetrics to minimize operational overhead. Mimir's superpower — surviving a single billion-series tenant — is also its cost: it's the most components you'll ever run for metrics.

**Try next:** run the monolithic container above, point one real Prometheus at it via `remote_write`, and watch `cortex_ingester_active_series` and the `/api/v1/status/tsdb` endpoint as your data lands — then decide honestly whether your series count justifies the microservices mode.
