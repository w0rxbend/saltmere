---
title: "ClickStack: wide events in ClickHouse as an observability stack"
date: 2026-08-13
track: observability
summary: "ClickStack bundles an OpenTelemetry collector, ClickHouse, and the HyperDX UI into one open-source observability stack — betting that wide events in a columnar store beat three separate silos for logs, metrics, and traces. Here's the architecture, the verified cost numbers behind the hype, a one-command quick start, and where the Grafana LGTM stack still wins."
reading_time: 5
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

The three-silo model — metrics in a TSDB, logs in a log store, traces in a trace store — is an artifact of storage engines, not of how debugging actually works. When a sensor fleet's ingestion latency spikes, the question is always the same: *which* requests, from *which* devices, hitting *which* code path? Answering it across three databases means three query languages and correlation by timestamp squinting. The **wide events** camp argues you should instead record one fat, context-rich event per unit of work — user, service, HTTP path, status, cache result, device ID — and put it in a store that can aggregate arbitrary columns fast. That store, increasingly, is ClickHouse: it already sat under Signoz, Uptrace, and half the in-house Datadog replacements. **ClickStack** is ClickHouse Inc. making that architecture official.

## What ClickStack actually is

ClickHouse acquired **HyperDX** in March 2025 and announced ClickStack that May. It's three known quantities glued together properly:

- an **OpenTelemetry collector** (custom distribution, preconfigured with ClickHouse-optimized schemas) as the only ingestion path — OTLP in, nothing proprietary;
- **ClickHouse** as the single store for logs, traces, metrics, and session replays;
- **HyperDX** as the UI: Lucene-style search for grep-brain moments, full SQL when you need joins, plus dashboards, alerts, and trace waterfalls.

Development happens at a real clip — the HyperDX repo shipped **v2.34.0 on August 7, 2026**, and the all-in-one image moved from `docker.hyperdx.io` to `clickhouse/clickstack-all-in-one`. Everything is open source (HyperDX MIT, ClickHouse and the collector Apache-2.0), with a managed version now available in ClickHouse Cloud.

The load-bearing feature is ClickHouse's native **JSON column type**: wide events have sparse, ever-changing attribute sets, and the JSON type stores each path as a real subcolumn. ClickHouse's published numbers claim ~10x faster searches and ~100x less data scanned versus stuffing attributes into string blobs — which is what makes "just log everything with full context" economically survivable. High cardinality stops being a billing incident (as it is in a labels-based TSDB) and becomes just another column to `GROUP BY`.

## The cost numbers, sourced

The pitch is mostly a compression pitch, and the numbers hold up better than most vendor math because several are from operators:

- ClickHouse's internal "LogHouse" holds **43+ PB of OpenTelemetry data**; the team claims a **~200x cost reduction** versus their prior Datadog bill.
- Independent write-ups measuring columnar-vs-Lucene storage put ClickHouse at **12–19x better compression than Elasticsearch** with 5–30x faster analytical queries; Didi's published Elasticsearch migration saw 30% cost reduction and 4x query speedup.
- ClickHouse's own cost-optimization playbook is concrete about mechanism: Delta+ZSTD codecs on timestamps (~50% storage cut on those columns), then tiered storage moving parts older than a week to S3, so retention becomes an object-storage bill instead of a block-storage one.

Rule of thumb from people running it: a single 8-core/16 GB node handles 10–100 GB/day comfortably. For an IoT backend logging one wide event per MQTT ingest, that's a lot of fleet per node.

## Quick start: OTLP in, search out

One container gives you the whole stack — ClickHouse, the collector, HyperDX, and a MongoDB for app state:

```bash
docker run -p 8080:8080 -p 4317:4317 -p 4318:4318 \
  clickhouse/clickstack-all-in-one:latest
```

Open `http://localhost:8080`, create a user, and grab the **ingestion API key** from Team Settings. Then point any OTel SDK or collector at it:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
export OTEL_EXPORTER_OTLP_HEADERS="authorization=<YOUR_INGESTION_API_KEY>"
export OTEL_SERVICE_NAME=mqtt-ingest
python app.py   # any OTLP-instrumented process
```

Spans and logs appear in HyperDX within seconds, searchable with `level:error device_id:aq-*` or with SQL against the underlying `otel_logs` / `otel_traces` tables. For production the docs split the same components apart — collector fleet, ClickHouse cluster, stateless HyperDX — scaling ingest and query independently.

## Honest trade-offs vs the LGTM stack

This journal leans Grafana: [Loki's structured metadata](/articles/observability/2026-07-30-loki-3-structured-metadata-otel), [Tempo's TraceQL metrics](/articles/observability/2026-07-30-tempo-traceql-metrics), [native histograms](/articles/observability/2026-07-30-prometheus-native-histograms). Where does ClickStack actually beat it, and where not?

**ClickStack wins on:** unified querying — one table, real SQL joins across signals, versus LogQL + TraceQL + PromQL linked at the UI layer. High-cardinality analytics — `GROUP BY device_id` over millions of devices is ClickHouse's home turf and Prometheus's nightmare. Operational surface — two or three components versus the five-plus of a full LGTM deployment. Retention economics for *rich* data, because columnar compression works best exactly when events are wide.

**LGTM wins on:** metrics maturity — PromQL, recording rules, native histograms, and the entire exporter/alerting ecosystem have a decade of sharpening; ClickStack's metrics story is the youngest part of the stack. Alerting — Alertmanager routing/silencing is far ahead of HyperDX alerts. Dashboards — Grafana's community library has no equivalent. And cheap-and-shallow logging: if you mostly grep recent logs and rarely aggregate, Loki's index-almost-nothing model on object storage is hard to underprice.

The deeper difference is philosophical: LGTM keeps three purpose-built engines and correlates between them; ClickStack bets one general columnar engine plus wide events makes correlation a non-problem. If your debugging is aggregation-shaped ("p99 by firmware version by region"), the second bet pays off.

**Try next:** Run the all-in-one container next to your existing collector, add a second OTLP exporter so the same spans flow to both Tempo and ClickStack for a week, then take your last real incident and try answering it in each — time-to-answer, not feature lists, is the honest benchmark.
