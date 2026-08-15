---
title: "VictoriaMetrics and VictoriaLogs: A Leaner Alternative to Prometheus and Loki"
date: 2026-08-14
track: observability
summary: "VictoriaMetrics is a Prometheus-compatible time-series database that answers PromQL (and its superset MetricsQL) and accepts remote-write; VictoriaLogs is a single-binary log database with its own query language, LogsQL, positioned against Loki. Both ship as one Go binary — this article covers how they are deployed and what the published efficiency figures do and do not establish."
reading_time: 6
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

**Gist.** A Prometheus server under high metric cardinality consumes memory in proportion to the number of active series it holds in its head block, and a Loki deployment spreads read and write duties across distributor, ingester and querier processes that must be operated as a set. The VictoriaMetrics stack replaces both with two self-contained Go binaries: **VictoriaMetrics**, a time-series database (TSDB) that accepts Prometheus remote-write and answers PromQL, and **VictoriaLogs**, a log database queried through LogsQL. The cost of that consolidation is a second query dialect to learn on the logs side, a compatibility surface that is Prometheus-compatible rather than Prometheus-identical, and efficiency figures that are vendor-reported rather than independently reproduced.

## Release state as of mid-2026

VictoriaMetrics ships on a fast-moving **v1.x** line, alongside a separate **long-term support (LTS)** line for deployments that prefer a slower upgrade cadence; the exact current tags are on the [GitHub releases page](https://github.com/VictoriaMetrics/VictoriaMetrics/releases) rather than restated here, because they turn over in weeks. VictoriaLogs left beta with **v1.0.0 in November 2024** and has since carried its own independent v1.x series. The practical consequence is that the logs half is no longer the experimental component of the pair: it is versioned and released as a product in its own right.

## VictoriaMetrics: compatible at the protocol, not identical in shape

Compatibility is expressed at three specific interfaces, and each one matters for a different part of an existing installation.

- **Ingestion.** VictoriaMetrics accepts the Prometheus **remote-write** protocol, so existing scrape configuration, relabelling rules and service discovery remain in the Prometheus process. Only the destination changes.
- **Query.** It exposes `/api/v1/query`, the Prometheus HTTP API path, and evaluates **PromQL** against it.
- **Grafana.** Because the query endpoint matches, Grafana treats the instance as a Prometheus data source, so dashboards and alert expressions carry over unmodified.

Redirecting an existing Prometheus is a single block:

```yaml
# prometheus.yml
remote_write:
  - url: http://vmsingle:8428/api/v1/write
```

The single-node binary is the entire installation — there is no companion process, no object-store sidecar and no external coordination service:

```bash
./victoria-metrics-prod \
  -storageDataPath=/var/lib/victoria-metrics \
  -retentionPeriod=12   # months
# listens on :8428 for both ingestion and queries
```

**`-retentionPeriod` is interpreted in months** in the form shown above, and it is the flag that governs how long samples survive; `-storageDataPath` is the only state the process owns, which makes backup and relocation a directory-level operation.

For larger installations the cluster version decomposes the same functionality into three roles — **`vminsert`, `vmstorage` and `vmselect`** — so that ingestion capacity, retained data volume and query concurrency are scaled independently rather than as one unit. The decomposition is the reason a single-node instance and a cluster present the same query API: only the placement of the work differs.

On top of PromQL, VictoriaMetrics implements **MetricsQL, a backward-compatible superset**. Backward compatibility is the load-bearing property: an expression that is valid PromQL remains valid and retains its meaning, so migration is additive. The following expression returns the five jobs with the highest five-minute request rate, with `default 0` supplying a value for series that would otherwise be absent from the result:

```promql
topk(5, sum(rate(http_requests_total[5m])) by (job) default 0)
```

## VictoriaLogs: LogsQL rather than LogQL

VictoriaLogs addresses the same problem as Loki but does not adopt its query language. **LogsQL leads with full-text word matching and then pipes the matched set through transformations**, which inverts the usual reading order of a log query: the filter is stated first and the aggregation last. Deployment mirrors the metrics side:

```bash
./victoria-logs-prod -storageDataPath=/var/lib/victoria-logs
# user interface and API on :9428
```

A query restricted to the last hour, one severity and one service, terminating in a count:

```logsql
_time:1h AND level:error AND service:checkout | stats count() as errors
```

Two structural points are worth separating. First, **word filters such as `level:error` are the form LogsQL is built around**, and they narrow the candidate set before any later stage runs, so the cost of the query is dominated by how much the filters admit rather than by how much was stored. Second, **`| stats` is a pipe stage**, the analogue of Loki's metric queries, applied to whatever the preceding filters admitted; the pipeline reads left to right in the manner of a shell pipeline rather than as a nested function application.

Ingestion is deliberately plural. VictoriaLogs accepts data from **Fluent Bit, Vector, Filebeat, OpenTelemetry, and Loki's own push API**. The last of these is the migration lever: a shipper already configured to push to Loki can be repointed without rewriting its output stage, which decouples the decision to change log storage from the decision to change log collection.

## What the efficiency figures establish

VictoriaMetrics is marketed on reduced memory use and improved compression. The figures in circulation deserve precise attribution. An independent [Last9 write-up](https://last9.io/blog/prometheus-vs-victoriametrics/) relays VictoriaMetrics' own benchmark numbers — approximately **4.3 GB of resident memory against roughly 14 GB for Prometheus** on the same `node_exporter` workload, a ratio near 3x, and **up to 10x better compression** — and states explicitly that these are vendor-reported results from a `node_exporter` benchmark, not third-party-verified measurements.

The distinction is not pedantry. A `node_exporter` workload has a particular series shape and a particular label churn rate, and both are the inputs that determine memory residency and compression ratio. The architectural elements cited for the difference — a **log-structured merge (LSM) tree**, per-column compression, and handling of high-cardinality churn that differs from Prometheus' head block — are structural properties, but the magnitude of their effect is workload-dependent. A ratio measured on one exporter's output is not a bound on any other workload, and quoting it in a capacity plan converts a benchmark observation into a commitment it cannot support. The defensible procedure is measurement against the actual metric set.

## When consolidation is the argument

The case for switching is operational rather than a claim that Prometheus is deficient. It reduces to three concrete substitutions: one binary in place of Prometheus plus Thanos or Mimir for long-term storage; one binary in place of Loki's distributor, ingester and querier processes; and a memory footprint that has been reported lower under cardinality that exhausts a single Prometheus process. Where a single modest Prometheus is operating within its memory budget, none of these substitutions applies and the migration returns nothing. Where an installation is already assembling remote-storage backends or operating Loki's process set, the stack collapses those parts into two processes.

## Pitfalls

- **Quoting the ~3x memory or 10x compression ratio in a capacity plan.** The numbers are vendor-reported from a `node_exporter` benchmark; a workload with different series shape or label churn has no reason to reproduce them, and the plan will be sized against a measurement that was never taken on it.
- **Assuming Prometheus-compatible means Prometheus-identical.** Compatibility is documented at remote-write, `/api/v1/query` with PromQL, and the Grafana data source. Behaviour outside those three interfaces is not covered by that claim.
- **Writing MetricsQL in expressions intended to remain portable.** MetricsQL is a superset: PromQL runs on VictoriaMetrics, but a MetricsQL-only construct such as `default 0` does not run on Prometheus, so alert rules using it are no longer movable back.
- **Reading `-retentionPeriod=12` as twelve days.** The value in the configuration above denotes months; a wrong unit silently sets retention off by a factor of thirty.
- **Treating VictoriaLogs as a drop-in for Loki queries.** The ingestion path is compatible via Loki's push API, but LogQL is not LogsQL — every saved query, dashboard panel and alert expression on the logs side requires rewriting.
- **Relocating a node without moving `-storageDataPath`.** That directory holds the entire state of the process; a fresh path starts an empty database with no error indicating the previous data was left behind.
