---
title: "RED metrics for free: the spanmetrics connector turns traces into dashboards"
date: 2026-07-30
track: observability
summary: "You already emit traces. The OpenTelemetry Collector's spanmetrics connector aggregates those spans into request count, error count, and a latency histogram — dimensioned by service, operation, and status — so you get RED dashboards and alerts without instrumenting a single metric by hand. Here's how the connector wiring works, the real config keys, and the PromQL to light up a panel."
reading_time: 6
tags: [observability, opentelemetry, collector, spanmetrics, red-metrics, prometheus]
sources:
  - title: "Span Metrics Connector — opentelemetry-collector-contrib README"
    url: "https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/connector/spanmetricsconnector/README.md"
  - title: "Span metrics — Grafana Tempo documentation"
    url: "https://grafana.com/docs/tempo/latest/metrics-generator/span_metrics/"
  - title: "Span Metrics connector — Splunk Observability Cloud documentation"
    url: "https://help.splunk.com/en/splunk-observability-cloud/manage-data/splunk-distribution-of-the-opentelemetry-collector/get-started-with-the-splunk-distribution-of-the-opentelemetry-collector/collector-components/connectors/span-metrics-connector"
  - title: "How to Configure the Span Metrics Connector for RED Metrics — OneUptime"
    url: "https://oneuptime.com/blog/post/2026-02-06-span-metrics-connector-red-metrics/view"
---

RED — **R**ate, **E**rrors, **D**uration — is the smallest useful summary of a service's health: how many requests per second, how many of them fail, and how long they take. The awkward part is that RED metrics and traces describe the *same events*. Every server span already carries a start time, a duration, a status code, and a service name. Instrumenting a separate `http_requests_total` counter alongside your traces means writing the same truth twice and hoping the two agree.

The OpenTelemetry Collector's **spanmetrics connector** removes that duplication. It sits inside the Collector, reads spans off your traces pipeline, and aggregates them into three metrics — a request counter, a duration histogram, and (optionally) an event counter — dimensioned by service, operation, span kind, and status. You point a metrics exporter at the output and you have RED dashboards for every instrumented service, derived from data you were already sending.

## What a "connector" actually is

A normal Collector component lives in one pipeline: receivers ingest, processors transform, exporters ship. A **connector** is different — it is simultaneously an *exporter* on one pipeline and a *receiver* on another. It bridges signals. The spanmetrics connector is the exporter end of a `traces` pipeline and the receiver end of a `metrics` pipeline. Spans flow in as traces; metric data points flow out as metrics. No spans leave the connector as spans — it consumes them for aggregation and emits only the derived metrics. (You almost always keep a second traces exporter for the spans themselves.)

## Processor → connector: mind the migration

If you find an old blog wiring up a `spanmetrics` *processor*, stop. That processor is **deprecated and removed** from the contrib distribution; the connector replaced it. The move wasn't just packaging — the connector introduced deliberate breaking changes over the processor:

- The `operation` dimension was renamed **`span.name`**.
- The `latency` histogram was renamed **`duration`**.
- The internal `_total` suffix on the calls metric was dropped (the Prometheus exporter re-adds `_total` for monotonic sums on the way out).

So config and dashboards written for the processor need porting, not copying. Anything current uses the connector.

## The metrics it emits

By default the connector produces two metrics (a third, `events`, is opt-in):

| Metric | Type | RED role |
|---|---|---|
| `calls` | monotonic sum | Rate **and** Errors (split by `status.code`) |
| `duration` | histogram | Duration |
| `events` | monotonic sum | span-event counts (opt-in) |

Each data point is labelled with the default dimensions **`service.name`**, **`span.name`**, **`span.kind`**, and **`status.code`**. That last one is what makes Errors free: failed spans carry `status.code = STATUS_CODE_ERROR`, so error rate is just a filtered slice of the same `calls` counter — you don't emit a separate error metric.

When these go through the Prometheus exporter, dots become underscores and the sum gains `_total`. With a `namespace` of `spanmetrics` and the default millisecond histogram unit you get:

- `spanmetrics_calls_total`
- `spanmetrics_duration_milliseconds_bucket` / `_sum` / `_count`

## Wiring it up

Here's a complete, minimal Collector config: OTLP traces in, spans fan out to both your trace backend and the connector, and the connector's metrics land on a Prometheus scrape endpoint.

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317

connectors:
  spanmetrics:
    namespace: spanmetrics
    histogram:
      explicit:
        buckets: [2ms, 8ms, 25ms, 100ms, 250ms, 750ms, 2s, 5s]
    dimensions:
      - name: http.request.method
      - name: http.route
    exclude_dimensions: [span.kind]      # trim cardinality you don't need
    exemplars:
      enabled: true                       # link metric points back to traces
    metrics_flush_interval: 15s
    metrics_expiration: 5m                # drop stale series after 5m idle

exporters:
  otlp/traces:                            # spans still go to your trace store
    endpoint: tempo:4317
    tls:
      insecure: true
  prometheus:
    endpoint: 0.0.0.0:8889

service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [otlp/traces, spanmetrics]   # spanmetrics is an exporter here
    metrics/spanmetrics:
      receivers: [spanmetrics]                 # ...and a receiver here
      exporters: [prometheus]
```

The key move is that `spanmetrics` appears as an exporter in the `traces` pipeline and as a receiver in the `metrics/spanmetrics` pipeline. That single component is the bridge.

A few keys worth knowing beyond the snippet:

- **`histogram.type`** — `explicit` (fixed buckets, shown above) or `exponential` (native/exponential histograms, no bucket tuning; set `histogram.exponential.max_size`). Choose buckets that straddle your SLO thresholds, or go exponential and stop guessing.
- **`dimensions`** — extra span or resource attributes to promote to labels. Powerful and dangerous: every distinct value multiplies your series count. Never add unbounded attributes like `http.url` or a user ID.
- **`exclude_dimensions`** — drop defaults you don't query on to cut cardinality.
- **`metrics_flush_interval`** — how often accumulated metrics are pushed downstream (default 60s; 15s gives fresher panels).
- **`metrics_expiration`** — evict series that stop receiving spans, so a decommissioned service doesn't linger forever.
- **`exemplars.enabled`** — attaches trace IDs to histogram points so a spike on a latency panel jumps straight to an offending trace. That metrics-to-traces jump is its own topic — see the Saltmere piece on exemplars — but flip it on here.

## The PromQL for a RED panel

Once `spanmetrics_calls_total` is scraped, RED is three queries. Request rate per service:

```promql
sum(rate(spanmetrics_calls_total[5m])) by (service_name)
```

Error ratio — the errors slice over the total, both from the *same* counter:

```promql
sum(rate(spanmetrics_calls_total{status_code="STATUS_CODE_ERROR"}[5m])) by (service_name)
/
sum(rate(spanmetrics_calls_total[5m])) by (service_name)
```

And p95 latency from the histogram:

```promql
histogram_quantile(0.95,
  sum(rate(spanmetrics_duration_milliseconds_bucket[5m])) by (le, service_name))
```

Alert on the error ratio crossing your budget, and you have paging built directly on trace data — no bespoke metric instrumentation, no drift between what the trace says and what the counter says.

## Where this sits among the alternatives

Two adjacent tools do related things. **Exemplars** go the other direction — they annotate metric points with trace IDs so you can pivot from a graph to a trace; the connector is a producer of those exemplars, not a competitor. **Tempo's TraceQL metrics** compute rates and latencies by querying raw stored traces at read time, which is flexible and needs no aggregation pipeline but is bounded by retention and query cost. The spanmetrics connector aggregates *at ingest*: cheap, always-on, low-cardinality time series that live in Prometheus indefinitely — the right tool when you want standing dashboards and alerts rather than ad-hoc exploration.

**Try next:** add the connector to a dev Collector, send a load test through your service, and confirm `spanmetrics_calls_total` appears in Prometheus before you touch a single hand-written metric.
