---
title: "SigNoz: An OpenTelemetry-Native APM You Can Self-Host"
date: 2026-08-14
track: observability
summary: "SigNoz is an open-source, OpenTelemetry-native APM that stores traces, metrics, logs, and exceptions in one ClickHouse backend — a self-hostable Datadog alternative you point your OTLP exporter at and nothing else."
reading_time: 5
tags: [signoz, opentelemetry, otlp, clickhouse, apm, observability]
sources:
  - title: "SigNoz — Docker self-host install (official docs)"
    url: "https://signoz.io/docs/install/docker/"
  - title: "SigNoz/signoz — GitHub"
    url: "https://github.com/SigNoz/signoz"
  - title: "SigNoz LICENSE (MIT Expat + enterprise directories)"
    url: "https://github.com/SigNoz/signoz/blob/main/LICENSE"
  - title: "SigNoz as a self-hosted OpenTelemetry backend (OneUptime, Feb 2026)"
    url: "https://oneuptime.com/blog/post/2026-02-06-signoz-self-hosted-opentelemetry-backend/view"
---

Most self-hosted observability stacks are an assembly job: a collector, Prometheus for metrics, something like Tempo for traces, Loki for logs, Grafana on top, and glue to correlate them. **SigNoz** collapses that into one product. It ingests everything over OTLP, stores traces, metrics, logs, and exceptions in a single **ClickHouse** backend, and ships a UI that does service maps, RED metrics, trace waterfalls, and log search out of the box. The pitch is blunt: a self-hostable alternative to Datadog.

## OpenTelemetry-native, not OpenTelemetry-compatible

The distinction matters. SigNoz does not have a proprietary agent you install and a "we also accept OTLP" side door. OTLP *is* the native protocol. A bundled SigNoz OTel Collector listens on the standard ports — **4317** for OTLP/gRPC and **4318** for OTLP/HTTP — and every signal (traces, metrics, logs) rides the same wire. There is no vendor SDK to adopt. If your app already emits OpenTelemetry, pointing it at SigNoz is a one-line change:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4317"
export OTEL_EXPORTER_OTLP_PROTOCOL="grpc"
export OTEL_SERVICE_NAME="checkout-api"
```

Because the ingestion path is stock OTLP, you can also keep your own OpenTelemetry Collector in front and add SigNoz as just another exporter — no rip-and-replace.

## Why ClickHouse under the hood

Traces and logs are wide, high-cardinality, append-only events — exactly what a columnar OLAP engine is built for. ClickHouse gives SigNoz aggregation queries like "p99 latency by endpoint by version over the last 6 hours" that stay fast on billions of spans, and columnar compression keeps rich, wide events cheap to retain. It also means one query engine and real SQL across all three signals, rather than PromQL for metrics, TraceQL for traces, and LogQL for logs stitched together at the dashboard layer. Exceptions get first-class treatment too — stack traces are grouped and linked back to the trace that produced them.

## Standing it up

The self-host path is a git clone and a `docker compose up`. As of mid-2026 the current release is the **v0.127.x** series (v0.127.0 shipped June 2026).

```bash
git clone -b main https://github.com/SigNoz/signoz.git
cd signoz/deploy/docker
docker compose up -d --remove-orphans
```

Watch it come up, then open the UI:

```bash
docker compose logs -f --tail=50
# UI at http://localhost:8080
```

Give Docker at least 4 GB of memory — ClickHouse and ZooKeeper want headroom. For clusters there is a Helm chart; the same OTLP endpoints apply, you just target the collector's service DNS instead of `localhost`.

## The license, stated plainly

SigNoz is open-core, and the details are worth getting right rather than guessing. The main repository is licensed **MIT (Expat)** for everything outside the `ee/` and `cmd/enterprise/` directories; code inside those two directories is covered by a separate SigNoz **Enterprise license**. In practice the core traces/metrics/logs platform you self-host is MIT, and the enterprise-only features (SSO, more granular RBAC, and similar) live behind the enterprise license. That is a genuinely permissive core — a real difference from tools that relicensed under BSL/SSPL — but confirm the current `LICENSE` file for your version before you build a commercial product on top of it.

## Where it fits

SigNoz is the strongest choice when you want one box that answers trace, metric, and log questions with SQL and no per-host billing. Its metrics maturity is younger than Prometheus's decade-hardened ecosystem, and Grafana's dashboard library has no equivalent yet — so if you live in PromQL and community dashboards, weigh that. But for a team that emits OpenTelemetry and wants Datadog-shaped answers without the invoice, the OTLP-in, ClickHouse-out design is hard to beat.

**Try next:** Spin up the Docker stack, set `OTEL_EXPORTER_OTLP_ENDPOINT` on one instrumented service, generate traffic, and see how long it takes to find your slowest endpoint's p99 by version — that time-to-answer is the honest benchmark against your current stack.
