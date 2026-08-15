---
title: "VictoriaMetrics: a Prometheus-compatible time-series database for high-cardinality workloads"
date: 2026-08-13
track: observability
summary: "A single Prometheus server holds its index and recent samples in memory, so high-churn Kubernetes and IoT metrics push it into RAM and disk pressure; VictoriaMetrics accepts the same remote_write protocol and serves the same query API, with a different storage engine, MetricsQL extensions, and an optional sharded cluster mode."
reading_time: 5
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

**Gist.** A vanilla Prometheus server keeps its most recent block — the head — and that block's index in memory, so workloads that create unique label combinations at a high rate — restarting Kubernetes pods, large device fleets — raise resident memory and local retention cost faster than sample volume alone would suggest. VictoriaMetrics replaces that storage layer while preserving the two interfaces that dashboards and alerts depend on: the Prometheus `remote_write` ingestion protocol and the Prometheus query API. The cost is a separate codebase from upstream Prometheus, with its own release cadence and its own behaviour at the edges of PromQL semantics, which has to be validated before a cutover.

## The unit of cost is the series, not the sample

A time series is identified by a metric name plus a set of label key/value pairs. Two samples belong to the same series only if that entire set matches. **Churn rate** — the rate at which new series identifiers appear — is therefore the dimension that hurts, because every distinct identifier needs an entry in the inverted index that maps label pairs to series, and that index is what a query planner walks before it touches a single sample. A pod restarting with a new `pod` label value produces a new series even though the metric and the workload are unchanged. A fleet of gateways that embed a firmware version or a session identifier in a label does the same.

This is why disk sizing based on "samples per second times bytes per sample" underestimates the real footprint of a Kubernetes or IoT deployment: the index grows with distinct series, not with sample count, and the index is the part that must be consulted per query.

## The storage model

VictoriaMetrics uses a log-structured merge (LSM) style design specialised for time series. Incoming samples accumulate in memory, are flushed to **immutable parts** on disk, and background merges combine small parts into larger ones. Because parts are immutable, a merge never mutates data a reader is using, and **snapshots are cheap**: a snapshot records the set of parts that constitute a point in time rather than copying their bytes.

Storage is columnar in the sense that timestamps and values for a series are encoded separately. Timestamps in a scrape-driven workload are close to an arithmetic progression, so **delta-of-delta encoding** reduces most of them to a value near zero before a general-purpose compressor runs; values in gauges and counters are similarly correlated with their predecessors. An uncompressed sample is a 64-bit timestamp and a 64-bit value, 16 bytes; on scrape-driven data both fields compress to a small fraction of that. The project's own published comparison claims **up to 7x less RAM and up to 7x less storage space** than Prometheus, Thanos or Cortex on high-cardinality workloads. These are vendor figures on vendor workloads; the number that matters for a given deployment is the one measured by running both backends against the same scrape set for a day and comparing data directory sizes.

Two operational consequences follow from the design rather than from any benchmark. VictoriaMetrics does not keep a Prometheus-style write-ahead log, so **restart does not begin with a WAL replay** whose duration scales with the in-memory head; the corresponding cost is that samples buffered in memory and not yet flushed are lost on an unclean shutdown. And the inverted index is built for high churn rate, which is the case where the index — not the sample store — dominates.

## Ingestion

The single-node binary listens on port **8428** and accepts Prometheus `remote_write` at `/api/v1/write`. An existing Prometheus needs one configuration block; nothing about its scrape configuration changes.

```yaml
# prometheus.yml — forward every scraped sample to VictoriaMetrics
remote_write:
  - url: http://victoriametrics:8428/api/v1/write

scrape_configs:
  - job_name: air-quality-fleet
    static_configs:
      - targets: ['gateway-01:9100', 'gateway-02:9100']
```

`vmagent` can replace the scraping Prometheus entirely. It reads Prometheus-format scrape configuration, **buffers to disk when the backend is unreachable** and replays on recovery — store-and-forward, the same shape as an IoT uplink queue — and can fan the same stream out to several remote endpoints. Relabelling at the agent is the cheapest place to attack cardinality, because a label dropped before transmission never creates a series downstream:

```yaml
# vmagent: drop a high-churn label before it reaches storage
- job_name: fleet
  static_configs:
    - targets: ['gateway-01:9100']
  metric_relabel_configs:
    - regex: 'session_id|firmware_build'
      action: labeldrop
```

Grafana queries VictoriaMetrics through its ordinary Prometheus datasource, so dashboards and their panel expressions are unchanged.

## MetricsQL

MetricsQL is documented as backwards-compatible with PromQL: PromQL expressions parse, and the extensions are additive. Compatibility is at the level of the language, not of every evaluation result — MetricsQL deliberately deviates from PromQL on some semantics, which is why a cutover has to be checked against the specific rules and dashboards in use rather than assumed.

```promql
# rate() with no lookbehind window: the evaluation step is used
rate(node_network_receive_bytes_total)

# retain the metric name across arithmetic
(process_resident_memory_bytes / 1024 / 1024) keep_metric_names

# substitute a constant series where data is missing
up{job="fleet"} default 0
```

`keep_metric_names` addresses the PromQL behaviour where arithmetic strips the metric name, which turns two derived expressions into an unnamed pair that collides. `default` supplies a fallback where a series is absent rather than zero. `WITH` templates factor repeated subexpressions out of a long expression. The dependency this introduces runs one way: an expression using MetricsQL extensions is not portable back to Prometheus.

## Single-node and cluster topologies

| | Single-node | Cluster |
|---|---|---|
| Binary | one `victoria-metrics` | `vminsert` + `vmselect` + `vmstorage` |
| Scales by | vertically | horizontally, by adding `vmstorage` nodes |
| Multi-tenancy | no | yes, tenant identifier in the URL path |
| Applies when | one machine's capacity suffices | multiple tenants, high availability, sharded ingest |

Cluster mode splits three roles that the single binary combines. **`vminsert` is stateless and shards writes across storage nodes**; **`vmstorage` holds the data and is the only stateful tier**; **`vmselect` is stateless and fans a query out to every storage node**, merging the partial results. Each tier scales independently, and the tenant identifier appears in the request path rather than as a label.

The split has a consequence for query semantics: because `vmselect` merges results from all `vmstorage` nodes, an unavailable storage node yields a partial answer unless the deployment is configured to reject it. Series are distributed across storage nodes, so a query touching many series touches many nodes.

## Pitfalls

- Sizing a migration from sample rate alone understates memory and disk, because the inverted index grows with distinct series identifiers and a high churn rate multiplies those independently of sample volume.
- A recording or alerting rule that uses a MetricsQL extension cannot be evaluated by upstream Prometheus, so a rollback to Prometheus fails at rule-load time rather than silently.
- Arithmetic in PromQL drops the metric name; adding `keep_metric_names` to fix a duplicate-series error changes the output labels, which can break a Grafana panel legend or an alert label matcher that was written against the unnamed result.
- Relying on the published "up to 7x" disk and RAM figures for capacity planning substitutes a vendor benchmark for a measurement of the specific label set in use.
- In cluster mode a `vmstorage` node that is down produces partial query results rather than an error unless partial responses are disabled, so a dashboard can show a plausible but incomplete series.
- Removing Prometheus in favour of `vmagent` moves scrape-failure and staleness behaviour to a different component; alerts written against Prometheus scrape metadata metrics have to be rechecked against what `vmagent` exposes.
