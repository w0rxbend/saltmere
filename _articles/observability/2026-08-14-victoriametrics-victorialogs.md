---
title: "VictoriaMetrics and VictoriaLogs: A Leaner Alternative to Prometheus and Loki"
date: 2026-08-14
track: observability
summary: "VictoriaMetrics is a Prometheus-compatible TSDB that speaks PromQL (plus MetricsQL) and ingests remote-write, while VictoriaLogs is a single-binary log database with its own LogsQL, positioned against Loki. Both ship as one Go binary — here's how to run them and what the efficiency claims really say."
reading_time: 5
tags: [victoriametrics, victorialogs, prometheus, loki, tsdb, logsql]
sources:
  - title: "VictoriaMetrics: Single-node version — docs"
    url: "https://docs.victoriametrics.com/victoriametrics/single-server-victoriametrics/"
  - title: "VictoriaMetrics/VictoriaMetrics — GitHub releases"
    url: "https://github.com/VictoriaMetrics/VictoriaMetrics/releases"
  - title: "VictoriaLogs — docs"
    url: "https://docs.victoriametrics.com/victorialogs/"
  - title: "Prometheus vs. VictoriaMetrics — Last9 (independent write-up)"
    url: "https://last9.io/blog/prometheus-vs-victoriametrics/"
---

If your Prometheus is eating RAM under high cardinality, or your Loki queries time out, the VictoriaMetrics stack is the drop-in most teams reach for first. Two components: **VictoriaMetrics** for metrics (a Prometheus-compatible time-series database) and **VictoriaLogs** for logs. Both are written in Go, both ship as a single static binary with no external dependencies, and both scale from a laptop to a cluster.

## Where things stand in 2026

VictoriaMetrics is on **v1.145.0** (June 5, 2026), with an LTS line at **v1.136.x** if you prefer slower churn. VictoriaLogs went GA with **v1.0.0 on November 12, 2024** — that release note declared it "ready for production" after ~18 months of beta — and is now on **v1.51.0** (June 17, 2026). So VictoriaLogs is no longer the experimental half of the pair; it's a stable, versioned product.

## VictoriaMetrics: Prometheus-compatible, not Prometheus-shaped

The compatibility is genuine. VictoriaMetrics accepts Prometheus **remote-write**, exposes a `/api/v1/query` endpoint that answers **PromQL**, and Grafana treats it as a Prometheus data source. You keep your existing scrape config, dashboards, and alerts; you just change where the data lands. Point Prometheus at it:

```yaml
# prometheus.yml
remote_write:
  - url: http://vmsingle:8428/api/v1/write
```

Run the single-node binary — that's the whole install:

```bash
./victoria-metrics-prod \
  -storageDataPath=/var/lib/victoria-metrics \
  -retentionPeriod=12   # months
# listens on :8428 for ingest and queries
```

For scale, the cluster version splits into `vminsert`, `vmstorage`, and `vmselect` so you scale ingest, storage, and query independently. On top of PromQL, VictoriaMetrics adds **MetricsQL**, a backward-compatible superset with quality-of-life functions. This query returns the top 5 jobs by 5-minute request rate — the `default 0` and `topk` ergonomics come from MetricsQL:

```promql
topk(5, sum(rate(http_requests_total[5m])) by (job) default 0)
```

## VictoriaLogs: LogsQL instead of LogQL

VictoriaLogs targets the same job as Loki but with a different query language, **LogsQL**, that leads with full-text word matching and pipes results through transformations. Run it the same way — one binary:

```bash
./victoria-logs-prod -storageDataPath=/var/lib/victoria-logs
# UI and API on :9428
```

A LogsQL query filtering errors for one service over the last hour and counting them:

```logsql
_time:1h AND level:error AND service:checkout | stats count() as errors
```

Word filters (`level:error`) are indexed and cheap; the `| stats` pipe aggregates like Loki's metric queries but reads more like a shell pipeline. VictoriaLogs ingests from the usual suspects — Fluent Bit, Vector, Filebeat, OpenTelemetry, and Loki's own push API — so you can point existing shippers at it without rewriting your pipeline.

## About the efficiency claims

VictoriaMetrics markets itself on lower RAM and better compression, and those claims are real but worth attributing honestly. An independent [Last9 write-up](https://last9.io/blog/prometheus-vs-victoriametrics/) relays VictoriaMetrics' own benchmark figures — roughly **4.3 GB of RAM versus ~14 GB for Prometheus** on the same node_exporter workload (~3x), and up to **10x better compression** — and explicitly notes these are vendor-reported numbers from a `node_exporter` benchmark, not third-party-verified results. The architectural reasons are sound (a log-structured merge tree, per-column compression, and a design that handles high-cardinality churn better than Prometheus' head block), but treat any single ratio as "your mileage will vary." Run it against your own metrics before you quote a number in a capacity plan.

## When it's worth switching

The pitch isn't "Prometheus is bad." It's operational: one binary instead of Prometheus + Thanos/Mimir for long-term storage, one binary instead of Loki's distributor/ingester/querier sprawl, and a smaller memory footprint under cardinality that would OOM a vanilla Prometheus. If you're happy on a modest single Prometheus, there's little to gain. If you're fighting scale or gluing together remote-storage backends, the VictoriaMetrics stack collapses a lot of moving parts into two processes.

**Try next:** Spin up `victoria-metrics-prod` locally, add the `remote_write` block to an existing Prometheus, and diff the RSS of both processes after an hour of your real scrape targets — the honest benchmark is your own workload.
