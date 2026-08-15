---
title: "The spanmetrics connector: deriving RED metrics from trace data"
date: 2026-07-30
track: observability
summary: "The OpenTelemetry Collector's spanmetrics connector aggregates spans into a request counter, a duration histogram, and an optional span-event counter — dimensioned by service, span name, span kind, and status code — so RED dashboards and alerts derive from trace data rather than from separately instrumented metrics. This article covers the connector wiring, the configuration keys, and the PromQL that reads the result."
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

**Gist.** Rate, errors and duration (RED) metrics and distributed traces describe the same events, so instrumenting a request counter alongside spans states the same truth twice and admits drift between the two. The OpenTelemetry Collector's **spanmetrics connector** aggregates spans in flight into a request counter and a latency histogram, labelled by service, span name, span kind and status code, so the metrics are a projection of the trace stream rather than an independent measurement. The cost is cardinality and memory: the connector holds one accumulating series per distinct label combination inside the Collector process, and every dimension added multiplies that set.

## What a connector is

A conventional Collector component belongs to exactly one pipeline: receivers ingest, processors transform, exporters ship. A **connector occupies two pipelines at once — it is an exporter on one and a receiver on another** — and therefore bridges signal types. The spanmetrics connector terminates a `traces` pipeline and originates a `metrics` pipeline. Spans enter; metric data points leave. **The connector emits no spans**, so a traces pipeline that lists only the connector as its exporter sends nothing to a trace store; a second trace exporter is normally configured alongside it.

## Processor to connector

Older material configures a `spanmetrics` *processor*. That processor is **deprecated and removed** from the contrib distribution, and the connector that replaced it changed names rather than preserving them:

- The `operation` dimension was renamed **`span.name`**.
- The `latency` histogram was renamed **`duration`**.
- The internal `_total` suffix on the calls metric was dropped; the Prometheus exporter re-adds `_total` for monotonic sums on the way out.

Processor-era configuration and dashboards therefore require porting, not copying — a dashboard querying `latency_bucket` or grouping by `operation` returns empty series against a connector deployment, with no error to explain the emptiness.

## Emitted metrics

By default the connector produces two metrics; a third, `events`, is opt-in.

| Metric | Type | RED role |
|---|---|---|
| `calls` | monotonic sum | Rate **and** errors (split by `status.code`) |
| `duration` | histogram | Duration |
| `events` | monotonic sum | span-event counts (opt-in) |

Each data point carries the default dimensions **`service.name`**, **`span.name`**, **`span.kind`** and **`status.code`**. The last of these is what makes the errors term free: **a failed span carries `status.code = STATUS_CODE_ERROR`, so the error rate is a filtered slice of the same `calls` counter** rather than a separate metric. Numerator and denominator of an error ratio are consequently drawn from one series family and cannot disagree about the request count.

Passing through the Prometheus exporter, dots become underscores and the monotonic sum gains `_total`. With a `namespace` of `spanmetrics` and the default millisecond histogram unit, the exposed names are:

- `spanmetrics_calls_total`
- `spanmetrics_duration_milliseconds_bucket` / `_sum` / `_count`

## Wiring

A minimal Collector configuration: OpenTelemetry Protocol (OTLP) traces arrive, spans fan out to both the trace store and the connector, and the connector's metrics are exposed for a Prometheus scrape.

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
    exclude_dimensions: [span.kind]      # removes a default label from every series
    exemplars:
      enabled: true                       # attaches trace IDs to metric points
    metrics_flush_interval: 15s
    metrics_expiration: 5m                # evicts series idle for 5m

exporters:
  otlp/traces:                            # spans still reach the trace store
    endpoint: tempo:4317
    tls:
      insecure: true
  prometheus:
    endpoint: 0.0.0.0:8889

service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [otlp/traces, spanmetrics]   # exporter position
    metrics/spanmetrics:
      receivers: [spanmetrics]                 # receiver position
      exporters: [prometheus]
```

The load-bearing detail is that **the same `spanmetrics` component name appears once as an exporter and once as a receiver**. Omitting either half leaves the component defined but inert: with no traces pipeline referencing it, no spans arrive; with no metrics pipeline referencing it, the aggregated points have nowhere to go.

Keys beyond the snippet:

- **`histogram.type`** — `explicit` uses the fixed bucket boundaries shown above; `exponential` uses exponential histograms with a bucket count governed by `histogram.exponential.max_size` and requires no boundary selection. Explicit buckets determine quantile resolution: **a quantile estimate is only as precise as the bucket edge nearest the service level objective (SLO) threshold**, so boundaries that straddle the threshold matter more than the number of boundaries.
- **`dimensions`** — additional span or resource attributes promoted to labels. Every distinct value of an added dimension multiplies the series count, so unbounded attributes such as a full URL or a user identifier are the direct route to memory exhaustion in the Collector.
- **`exclude_dimensions`** — removes default dimensions, reducing the label set that generates series.
- **`metrics_flush_interval`** — how often accumulated metrics are pushed downstream; the default is 60s, and a shorter interval shortens the delay before a change appears on a panel.
- **`metrics_expiration`** — evicts series that have stopped receiving spans, so a decommissioned service's labels stop being emitted.
- **`exemplars.enabled`** — attaches trace identifiers to the emitted data points, allowing a latency panel to link to a trace that contributed to a bucket. That mechanism has its own article on Saltmere.

## PromQL for a RED panel

Once `spanmetrics_calls_total` is scraped, the three RED terms are three queries. Request rate per service:

```promql
sum(rate(spanmetrics_calls_total[5m])) by (service_name)
```

Error ratio, with numerator and denominator drawn from the same counter:

```promql
sum(rate(spanmetrics_calls_total{status_code="STATUS_CODE_ERROR"}[5m])) by (service_name)
/
sum(rate(spanmetrics_calls_total[5m])) by (service_name)
```

The 95th-percentile latency from the histogram:

```promql
histogram_quantile(0.95,
  sum(rate(spanmetrics_duration_milliseconds_bucket[5m])) by (le, service_name))
```

Alerting on the error ratio therefore pages on trace-derived data, with no separately instrumented counter that could diverge from the spans.

## Position among the alternatives

Two adjacent mechanisms address related problems. **Exemplars** run in the opposite direction: they annotate metric points with trace identifiers so a graph links to a trace, and the connector is a producer of exemplars rather than an alternative to them. **Tempo's TraceQL metrics** compute rates and latencies by querying stored traces at read time, which requires no aggregation pipeline but is bounded by trace retention and by query cost at each evaluation. The spanmetrics connector aggregates **at ingest**, producing time series whose retention is the metric store's rather than the trace store's, and whose query cost is independent of trace volume.

## Pitfalls

- A traces pipeline listing `spanmetrics` as its only exporter delivers no spans to any trace backend, because the connector consumes spans and emits only metrics. The symptom is working RED dashboards with an empty trace search.
- A dimension built from an unbounded attribute — a URL containing identifiers, a user identifier, a raw path — creates one series per distinct value, held in Collector memory until `metrics_expiration` elapses. The symptom is Collector memory growth proportional to distinct request paths.
- Configuration ported from the removed `spanmetrics` processor queries `latency_*` and groups by `operation`. Those series do not exist under the connector, so panels render empty rather than erroring.
- Omitting `metrics_expiration` leaves series for decommissioned services being emitted indefinitely, so a rate over those series stays at zero rather than disappearing, and a `by (service_name)` panel keeps a flat line for a service that no longer exists.
- Explicit histogram buckets whose boundaries do not straddle the SLO threshold make `histogram_quantile` interpolate across a wide bucket; the reported quantile then moves in steps tied to bucket width rather than tracking the true latency.
- `exclude_dimensions: [span.kind]` merges client and server spans for the same operation into one series, so a client-side timing that includes network latency is summed with the server-side timing of the same call.
