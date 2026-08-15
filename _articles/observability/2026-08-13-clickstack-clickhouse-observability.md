---
title: "ClickStack: wide events in ClickHouse as an observability stack"
date: 2026-08-13
track: observability
summary: "ClickStack bundles an OpenTelemetry collector, ClickHouse, and the HyperDX user interface into one open-source observability stack, on the premise that wide events in a columnar store replace three separate silos for logs, metrics, and traces. This article covers the architecture, the sourced cost figures, a single-container start, and the areas where the Grafana LGTM stack remains ahead."
reading_time: 6
tags: [clickstack, clickhouse, hyperdx, wide-events, opentelemetry]
sources:
  - title: "ClickStack: A High-Performance OSS Observability Stack on ClickHouse (May 2025)"
    url: "https://clickhouse.com/blog/clickstack-a-high-performance-oss-observability-stack-on-clickhouse"
  - title: "ClickStack overview — ClickHouse documentation"
    url: "https://clickhouse.com/docs/clickstack/overview"
  - title: "hyperdxio/hyperdx releases (GitHub)"
    url: "https://github.com/hyperdxio/hyperdx/releases"
  - title: "ClickStack: ClickHouse's New Observability Stack Unveiled — Dotan Horovits"
    url: "https://horovits.medium.com/clickstack-clickhouses-new-observability-stack-unveiled-73f129a179a3"
  - title: "What Is ClickStack? We Tested the Open-Source Datadog Alternative — Tasrie IT"
    url: "https://tasrieit.com/blog/what-is-clickstack-clickhouse-observability-explained-2026"
---

**Gist.** The conventional three-silo model — metrics in a time-series database (TSDB), logs in a log store, traces in a trace store — forces correlation across three engines and three query languages, with timestamps as the only join key. ClickStack replaces the three silos with one **wide event** per unit of work, stored in ClickHouse as a columnar table and queried with SQL, so correlation becomes a `WHERE` clause rather than a cross-system reconciliation. The cost is that everything now depends on one general-purpose analytical engine: metrics, alerting, and dashboarding are handled by components far younger than the Prometheus and Grafana equivalents they displace.

## The wide-event premise

A wide event records one row per unit of work, carrying the full context of that work — service, HTTP path, status, cache result, device identifier, firmware version — rather than splitting that context across a counter increment, a log line, and a span. The premise is that debugging questions are usually **aggregation-shaped**: which requests, from which devices, on which code path. Answering such a question needs a store that can group by an arbitrary column cheaply, which is what a columnar engine does. ClickHouse already sat beneath Signoz, Uptrace, and a number of in-house Datadog replacements before ClickHouse Inc. packaged the pattern.

## Components

ClickHouse acquired **HyperDX** in March 2025 and announced ClickStack that May. Three components:

- an **OpenTelemetry (OTel) collector** — a custom distribution preconfigured with ClickHouse-oriented schemas — as the sole ingestion path, speaking the OpenTelemetry Protocol (OTLP) and nothing proprietary;
- **ClickHouse** as the single store for logs, traces, metrics, and session replays;
- **HyperDX** as the interface: Lucene-style search, full SQL for joins, dashboards, alerts, and trace waterfalls.

The HyperDX repository continues to publish tagged releases, and the all-in-one image is distributed as `clickhouse/clickstack-all-in-one`. Licensing is open source throughout — HyperDX under MIT, ClickHouse and the collector under Apache-2.0 — with a managed variant in ClickHouse Cloud.

The load-bearing storage feature is ClickHouse's native **JSON column type**. Wide events have sparse and continually changing attribute sets; the JSON type stores **each JSON path as a distinct subcolumn**, so a query touching one attribute reads that attribute's column rather than parsing every event body. ClickHouse's published figures claim roughly **10x faster searches and about 100x less data scanned** compared with holding attributes in string blobs. The consequence that matters operationally: **high cardinality stops being a storage-model problem**. In a labels-based TSDB every distinct label value creates a new time series, so a device identifier as a label multiplies series count; in a columnar table a device identifier is one more column to `GROUP BY`, and its cost is the compressed size of that column.

## The cost figures, and their provenance

The argument is largely a compression argument, and several of the numbers come from operators rather than from marketing:

- ClickHouse's internal deployment, **LogHouse**, holds **petabytes of OpenTelemetry data**; the team claims a **~200x cost reduction** versus its prior Datadog spend. This is a self-reported figure from the vendor's own operations.
- Published comparisons of columnar storage against Lucene-based indexing report ClickHouse compressing observability data substantially better than Elasticsearch, with correspondingly faster analytical queries; the reported ratios vary widely by dataset and are not from a neutral benchmark. Didi's published Elasticsearch migration reports a **30% cost reduction and a 4x query speedup** — a narrower result than the compression ratios alone would suggest.
- ClickHouse's cost-optimization guidance is specific about mechanism rather than outcome: **Delta encoding followed by ZSTD compression on timestamp columns**, which exploits the near-monotonic ordering of timestamps to shrink those columns, and **tiered storage** that moves parts older than a configured age to S3. The second changes the shape of the bill: retention beyond the hot window is charged as object storage rather than block storage.

Reported operational sizing from practitioners: a single **8-core, 16 GB node handles 10–100 GB/day**. That range spans an order of magnitude and reflects event width and query load, not a benchmark.

## Single-container start

One container runs the whole stack — ClickHouse, the collector, HyperDX, and a MongoDB instance for application state:

```bash
docker run -p 8080:8080 -p 4317:4317 -p 4318:4318 \
  clickhouse/clickstack-all-in-one:latest
```

Port 4317 is OTLP over gRPC, 4318 is OTLP over HTTP, 8080 is the interface. After creating a user at `http://localhost:8080`, the **ingestion API key** is read from Team Settings and supplied as an OTLP header by any instrumented process:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
export OTEL_EXPORTER_OTLP_HEADERS="authorization=<INGESTION_API_KEY>"
export OTEL_SERVICE_NAME=mqtt-ingest
python app.py   # any OTLP-instrumented process
```

Spans and logs become searchable within seconds, either through Lucene-style expressions such as `level:error device_id:aq-*` or through SQL against the underlying `otel_logs` and `otel_traces` tables. The production topology documented by ClickHouse separates the same components — a collector fleet, a ClickHouse cluster, and stateless HyperDX instances — so ingest and query scale independently.

## Comparison with the LGTM stack

This journal has covered the Grafana side in detail: [Loki's structured metadata](/articles/observability/2026-07-30-loki-3-structured-metadata-otel), [Tempo's TraceQL metrics](/articles/observability/2026-07-30-tempo-traceql-metrics), and [Prometheus native histograms](/articles/observability/2026-07-30-prometheus-native-histograms).

**ClickStack is ahead on** unified querying: one table and genuine SQL joins across signals, rather than LogQL, TraceQL, and PromQL correlated at the interface layer. It is ahead on high-cardinality analytics, where grouping by a device identifier across millions of devices is a columnar scan rather than a series explosion. It presents a smaller operational surface — two or three components against the five or more of a full LGTM deployment. Its retention economics improve precisely as events get wider, because columnar compression benefits from repeated values in a column.

**LGTM is ahead on** metrics maturity: PromQL, recording rules, native histograms, and the exporter and alerting ecosystem have a decade of refinement behind them, and metrics are the youngest part of ClickStack. Alertmanager's routing and silencing exceed HyperDX alerting. Grafana's community dashboard library has no ClickStack equivalent. For shallow log usage — mostly grep over recent data, rarely aggregated — Loki's index-almost-nothing model over object storage is difficult to undercut, because ClickStack pays columnar write and merge costs for analytical capability that such a workload never exercises.

The structural difference is which correlation cost is paid: LGTM maintains three purpose-built engines and correlates between them at query time; ClickStack maintains one general columnar engine and makes correlation a property of the row.

## Pitfalls

- **Attributes written into a string body rather than a JSON column lose subcolumn extraction.** Symptom: queries scan the full event body and the reported ~100x scan reduction does not appear. Cause: the JSON type stores each path as a subcolumn only when the path exists as JSON, not as serialized text inside another field.
- **Treating LogHouse's ~200x figure as a portable estimate.** Symptom: projected savings that do not materialize. Cause: the number is ClickHouse Inc.'s own workload compared against its own prior Datadog contract; Didi's independently published migration reports 30%, not 200x.
- **Sizing from the 10–100 GB/day per-node figure without accounting for event width.** Symptom: an 8-core node that keeps up in staging falls behind in production. Cause: the range is practitioner rule-of-thumb spanning an order of magnitude, and wide events with many distinct paths consume more of it.
- **Expecting the all-in-one image to be a production topology.** Symptom: no way to scale ingest independently of query. Cause: the image colocates the collector, ClickHouse, HyperDX, and MongoDB in one container; the documented production layout separates them.
- **Migrating metrics first.** Symptom: missing recording-rule, exporter, and alert-routing equivalents. Cause: metrics and alerting are the least mature parts of ClickStack, while ingest and log or trace search are the parts the compression figures describe.
- **Assuming tiered storage is transparent to query latency.** Symptom: queries over older windows slow noticeably. Cause: parts older than the configured age reside in S3, and reads then include object-storage round trips rather than local block reads.
