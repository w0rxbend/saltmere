---
title: "VictoriaMetrics: a Prometheus-compatible TSDB built for scale"
date: 2026-08-13
track: observability
summary: "When a single Prometheus starts eating RAM and disk on high-cardinality IoT and Kubernetes metrics, VictoriaMetrics is the usual drop-in: same remote_write and PromQL, far tighter storage, MetricsQL on top, and a cluster mode when one node isn't enough."
reading_time: 6
tags: [victoriametrics, prometheus, tsdb, metricsql, remote-write]
sources:
  - title: "VictoriaMetrics single-node docs"
    url: "https://docs.victoriametrics.com/victoriametrics/single-server-victoriametrics/"
  - title: "VictoriaMetrics cluster docs"
    url: "https://docs.victoriametrics.com/victoriametrics/cluster-victoriametrics/"
  - title: "MetricsQL reference"
    url: "https://docs.victoriametrics.com/victoriametrics/metricsql/"
  - title: "VictoriaMetrics CHANGELOG 2026"
    url: "https://docs.victoriametrics.com/victoriametrics/changelog/changelog_2026/"
  - title: "vmagent — data ingestion"
    url: "https://docs.victoriametrics.com/victoriametrics/data-ingestion/vmagent/"
---

Vanilla Prometheus is excellent until scale bends it: high-churn Kubernetes pods and large IoT fleets push unique series into the millions, RAM balloons, and local TSDB retention gets expensive. VictoriaMetrics (current release **v1.149.0**, 5 Aug 2026) is the most common landing spot because it's a genuine drop-in — it speaks Prometheus `remote_write`, scrapes Prometheus targets, and serves the Prometheus query API — while claiming roughly **7x less disk and up to 7x less RAM** than Prometheus/Thanos/Cortex on the same workload. You keep Grafana, your dashboards, and your alerting rules; you swap the storage.

## The storage model, briefly

VictoriaMetrics uses an LSM-like design tuned for time series: samples land in memory, get flushed to immutable parts, and background merges compact them. Compression is the headline — per-column encoding of timestamps and values (delta-of-delta on timestamps, then general compression) squeezes typical metrics to around a byte or less per sample. The inverted index is built to survive **high churn rate** (short-lived series from restarting pods), which is precisely the case where Prometheus's head block and index cost hurt most. There's no WAL replay pause on restart, and instant snapshots make backups cheap.

## Ingestion: remote_write in, done

Point Prometheus (or better, `vmagent`) at the single-node write endpoint on port **8428**:

```yaml
# prometheus.yml — ship everything to VictoriaMetrics
remote_write:
  - url: http://victoriametrics:8428/api/v1/write

scrape_configs:
  - job_name: air-quality-fleet
    static_configs:
      - targets: ['gateway-01:9100', 'gateway-02:9100']
```

Better yet, drop Prometheus and let `vmagent` scrape directly — it's lighter, buffers to disk if the backend is unreachable (store-and-forward, familiar to anyone doing IoT uplinks), and can fan out to multiple remote endpoints. Grafana then queries VictoriaMetrics using its Prometheus datasource; nothing on the dashboard side changes.

## MetricsQL: PromQL plus the parts you wished it had

MetricsQL is a superset — valid PromQL runs unchanged — that removes common papercuts:

```promql
# rate() without a lookbehind window: uses the step automatically
rate(node_network_receive_bytes_total)

# keep the metric name after arithmetic (no "duplicate series" errors)
(process_resident_memory_bytes / 1024 / 1024) keep_metric_names

# fill gaps with a fallback series
up{job="fleet"} default 0
```

Other niceties: `default` for missing data, `WITH` templates to factor out repeated subexpressions, and gauge functions that don't force you to remember lookbehind rules. It's additive — you adopt the shortcuts you want and everything else stays PromQL.

## Single-node vs cluster

| | Single-node | Cluster |
|---|---|---|
| Binary | one `victoria-metrics` | `vminsert` + `vmselect` + `vmstorage` |
| Scales by | vertical (bigger box) | horizontal (add vmstorage nodes) |
| Multi-tenant | no | yes (tenant in the path) |
| When | up to millions of series, one node's worth | many tenants, HA, sharded ingest |

The single node is genuinely capable — teams run it to tens of millions of active series before splitting. Cluster mode separates ingestion (`vminsert` shards writes across storage nodes), storage (`vmstorage`, the stateful tier), and query (`vmselect`, stateless fan-out), so you scale each independently and get multi-tenancy via a tenant ID in the URL. Start single-node; move to cluster when one machine or HA forces it.

## Why teams actually migrate

It's rarely one feature. It's that the same metrics cost a fraction of the disk and memory, restarts are instant, the query API is a drop-in, and MetricsQL smooths over the daily annoyances — all without retraining anyone off PromQL and Grafana. The trade you accept: it's a different codebase from upstream Prometheus (its own release cadence and quirks), so validate your recording rules and any exotic PromQL against it before cutting over.

**Try next:** run `victoria-metrics` in Docker on `:8428`, add one `remote_write` block to an existing Prometheus, and diff a `rate()` panel in Grafana pointed at each backend — then check the on-disk size of the VM data dir versus your Prometheus TSDB after a day.
