---
title: "Exemplars: the sticky note a metric leaves for a trace"
date: 2026-07-26
track: observability
summary: "A histogram tells you P99 got worse; it can't tell you which request caused it. Exemplars attach a sampled trace_id to a metric sample so Grafana can jump straight from the graph to the offending trace. Here's the format, the flags, and the wiring."
reading_time: 5
tags: [exemplars, prometheus, opentelemetry, grafana, tracing, metrics, openmetrics]
sources:
  - title: "OpenMetrics specification (Exemplars)"
    url: "https://github.com/prometheus/OpenMetrics/blob/main/specification/OpenMetrics.md"
  - title: "Prometheus feature flags — exemplar-storage"
    url: "https://prometheus.io/docs/prometheus/latest/feature_flags/"
  - title: "Introduction to exemplars (Grafana docs)"
    url: "https://grafana.com/docs/grafana/latest/fundamentals/exemplars/"
  - title: "Configure and use exemplars (Grafana Cloud docs)"
    url: "https://grafana.com/docs/grafana-cloud/send-data/traces/configure/exemplars/"
  - title: "client_golang exemplars example"
    url: "https://github.com/prometheus/client_golang/blob/main/examples/exemplars/main.go"
---

You've stared at this graph before: a latency histogram, P99 line creeping up over the last ten minutes. Something got slow. The histogram is very sure about *that*. It has no opinion on *which request*. It's an aggregate — thousands of observations folded into bucket counts — and aggregates are, definitionally, information you already threw away the specific in order to get.

Meanwhile your tracing backend has exactly the request you want: full span tree, DB calls, exact duration. But you have no way to ask it "show me the one that pushed P99 over 400ms" — traces don't know what a percentile is either. Two systems, each holding half of the answer, with no wire between them.

Exemplars are that wire.

## What an exemplar actually is

An exemplar is a single sampled data point attached to a metric sample: a small set of labels (almost always including a `trace_id`) plus the exact value that was observed, and optionally a timestamp. It's not a summary or an aggregate — it's one specific measurement, kept alongside the bucket it landed in, as a receipt.

The OpenMetrics spec defines the syntax precisely. On a histogram, exemplars ride on the bucket line:

```
http_request_duration_seconds_bucket{le="0.5"} 129389 # {trace_id="a1b2c3d4e5f6"} 0.372 1690000000.928
```

Per the [OpenMetrics spec](https://github.com/prometheus/OpenMetrics/blob/main/specification/OpenMetrics.md), that trailing `# {...} value timestamp` block is the exemplar: the label set is capped at 128 UTF-8 characters combined, the sample value must fall inside the bucket's range, and each bucket line carries **at most one** exemplar — the client library keeps the most recent (or a randomly sampled) observation per bucket, it doesn't try to remember all of them. Exemplars are only legal on Histogram bucket samples, GaugeHistogram bucket samples, and Counter totals — you can't hang one off an arbitrary Gauge.

That last constraint is the whole design: exemplars aren't a general-purpose tracing sidecar, they're scoped to exactly the metric types where "which specific sample landed here" is a meaningful question — the ones a percentile can hide a slow outlier inside.

## The problem in one sentence

A histogram answers "how bad, in aggregate." An exemplar answers "show me one instance of how bad, and give me a key to look it up." Without it, going from "P99 regressed" to "here's the trace" means grepping logs by timestamp and hoping, or eyeballing the tracing UI's own latency histogram and guessing which span matches the metric dashboard's time window. With it, you click the dot.

## How they flow through the stack

Three independent pieces have to agree to make this work end to end:

| Stage | Component | Responsibility |
|---|---|---|
| Emit | App + OTel/Prometheus client library | Attach the active `trace_id` to the histogram observation |
| Expose | `/metrics` endpoint | Serialize the exemplar using OpenMetrics exposition format |
| Scrape + store | Prometheus | Parse the exemplar, persist it (opt-in) |
| Render | Grafana | Draw the exemplar as a marker on the graph, deep-link to the trace |

**Emit.** A Prometheus client library exposes exemplar support through an `ObserveWithExemplar`-style call (in Go, the histogram's `ExemplarObserver` interface). Inside a traced request handler you pull the current span's trace ID and pass it as the exemplar label:

```go
requestDuration := prometheus.NewHistogram(prometheus.HistogramOpts{
    Name:    "http_request_duration_seconds",
    Help:    "HTTP request duration in seconds.",
    Buckets: prometheus.DefBuckets,
})

func recordRequest(ctx context.Context, start time.Time) {
    span := trace.SpanContextFromContext(ctx)
    requestDuration.(prometheus.ExemplarObserver).ObserveWithExemplar(
        time.Since(start).Seconds(),
        prometheus.Labels{"trace_id": span.TraceID().String()},
    )
}
```

OpenTelemetry SDKs do the equivalent automatically for metrics recorded inside an active span context — the exemplar sampling and trace-context attachment happens in the SDK, not in application code, which is the whole point of using OTel instrumentation instead of hand-rolled client calls.

**Expose.** The metrics endpoint only serializes exemplars if the scrape negotiates the OpenMetrics text format (`Accept: application/openmetrics-text`) rather than the older plain Prometheus text format — exemplars don't exist in the legacy exposition format at all. This is invisible to you in practice: Prometheus requests OpenMetrics automatically, but if you're eyeballing `/metrics` with `curl` and not seeing exemplars, that's usually why.

**Scrape and store.** This is the step people forget, because it's an opt-in feature flag, not a default. Prometheus must be started with:

```
--enable-feature=exemplar-storage
```

Per the [Prometheus feature flags docs](https://prometheus.io/docs/prometheus/latest/feature_flags/), exemplar storage is a fixed-size, in-memory circular buffer — exemplars are not kept forever, and a single `trace_id`-only exemplar costs roughly 100 bytes. The buffer size is tunable in the config file:

```yaml
storage:
  exemplars:
    max_exemplars: 100000
```

Without the flag, Prometheus scrapes and stores the histogram normally and silently drops every exemplar line it parses. There's no warning — the metric just looks exemplar-free, which is the most common reason "exemplars aren't showing up" turns out to be a missing flag rather than a missing trace_id.

**Render.** In Grafana, exemplar support is a property of the Prometheus data source, on by default once the underlying data has exemplars. The [Grafana docs](https://grafana.com/docs/grafana/latest/fundamentals/exemplars/) describe them rendering as small marker points overlaid on the time series; hovering shows the trace ID and a button to jump into the configured trace data source. To turn that button into a real deep link, the data source's **Exemplars** config needs:

```yaml
exemplarTraceIdDestinations:
  - datasourceUid: tempo-uid
    name: trace_id
```

`name` must match the label the client library used (`trace_id` above), and `datasourceUid` points at your Tempo or Jaeger data source. Get the label name wrong and the dots render but the link goes nowhere.

## Enabling it end to end — the checklist

1. Instrument histograms with exemplars (native OTel SDK, or `ObserveWithExemplar` in a Prometheus client library), tagging the sample with the request's live `trace_id`.
2. Confirm `/metrics` serves OpenMetrics format — check for `# {trace_id=...}` lines when scraped with `Accept: application/openmetrics-text`.
3. Start Prometheus with `--enable-feature=exemplar-storage`; size the buffer via `storage.exemplars.max_exemplars` if the default is too small for your exemplar volume.
4. In Grafana's Prometheus data source, set `exemplarTraceIdDestinations` to point `trace_id` at your Tempo/Jaeger data source UID.
5. Query the histogram in Explore or a dashboard panel and confirm dots appear with working "Query with Tempo" links.

Each step fails silently if skipped, which is exactly why exemplars have a reputation for "not working" — the failure mode is always an empty state, never an error.

**Try next:** wire the exemplar's timestamp into a Tempo TraceQL query filtered to ±5s of the spike, so the deep link lands on a short list of candidate traces instead of a single point-in-time guess.
