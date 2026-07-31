---
title: "Delta vs cumulative: the OpenTelemetry metric setting that quietly breaks your dashboards"
date: 2026-07-31
track: observability
summary: "OTLP metrics carry an 'aggregation temporality' — cumulative (total since start) or delta (change since last export). Send the wrong one to your backend and rates come out garbage with no error anywhere. Here's how to pick, and how to convert when you picked wrong."
reading_time: 5
tags: [opentelemetry, metrics, prometheus, otlp, temporality]
sources:
  - title: "OpenTelemetry metrics: Delta vs. Cumulative temporality trade-offs — Grafana Labs"
    url: "https://grafana.com/blog/opentelemetry-metrics-a-guide-to-delta-vs-cumulative-temporality-trade-offs/"
  - title: "Producing Delta Temporality Metrics with OpenTelemetry — Datadog docs"
    url: "https://docs.datadoghq.com/opentelemetry/guide/otlp_delta_temporality/"
  - title: "OpenTelemetry Metrics Data Model — aggregation temporality (spec)"
    url: "https://opentelemetry.io/docs/specs/otel/metrics/data-model/"
---

You wire up an OpenTelemetry counter, ship it to your backend, and the graph is nonsense — a saw-tooth that resets, or a `rate()` that's flat when traffic clearly isn't. Nothing errored. The usual culprit is **aggregation temporality**: a property of every OTLP sum/histogram that says whether the number is a running total or an increment, and a mismatch between what your SDK sends and what your backend expects fails *silently*.

## The two temporalities

- **Cumulative:** the value is the total since the process (or metric) started. A request counter at 1,000 means "1,000 since startup." It only ever goes up, until a restart resets it to zero. This is **Prometheus's native model** — `rate()` and `increase()` are built to see an ever-rising line and compute the slope, and they specifically detect the reset-to-zero as a counter restart.
- **Delta:** the value is the change *since the last export*. The same counter reports "37" for this interval, then "52" for the next — each export stands alone and the numbers don't accumulate. This is what **StatsD/Datadog-style** systems want, because they do the summing server-side.

Neither is "correct" in the abstract; they're two encodings of the same information. The trouble is that a delta series fed to a cumulative-expecting backend looks like a counter that resets on *every* scrape, and a cumulative series fed to a delta pipeline gets summed on top of already-summed values. Both produce plausible-looking, completely wrong graphs.

## Pick to match the backend

The rule is simple once you know it: **choose temporality to match where the data is going.**

- Prometheus, Mimir, Thanos, and anything speaking PromQL → **cumulative**. It's the default in the OTel SDKs precisely because Prometheus interoperability is the common path.
- Datadog and other delta-native backends → **delta**, often required for counters to be interpreted correctly.

Set it once at the exporter. Via environment variable:

```bash
# cumulative (Prometheus-friendly) — usually the default
export OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative
# or delta, for a delta-native backend
export OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=delta
```

There's a third value, `lowmemory`, that uses delta for the metric instruments that are cheap to keep stateless (counters, histograms) and cumulative for the rest — a middle ground when the SDK's memory footprint matters.

## The trade behind the setting

It's not purely a compatibility knob; there's a real cost difference. **Cumulative** requires the SDK to hold running totals for every series in memory for the life of the process — more memory on the client, and a restart looks like a reset the backend has to reason about. **Delta** lets the client forget each interval after exporting, so it's lighter and plays nicely with ephemeral things like serverless functions that die between requests — but it pushes the burden of summing (and of handling late or duplicated exports) onto the backend, and a lost delta export is a permanently lost increment.

## When you're stuck with the wrong one

Sometimes you can't change the producer — a third-party service emits delta and you run Prometheus. Convert **in the Collector** instead of at the source:

```yaml
processors:
  deltatocumulative:            # delta in -> cumulative out (for Prometheus)
    max_stale: 5m
service:
  pipelines:
    metrics:
      processors: [deltatocumulative]
```

There's a `cumulativetodelta` processor for the reverse direction. Doing the conversion at the Collector means one place to fix it, rather than chasing SDK config across a dozen services.

**Try next:** point one OTel-instrumented service at a Prometheus backend with `TEMPORALITY_PREFERENCE=delta` on purpose, watch `rate()` on a counter go haywire, then flip it to `cumulative` and see the graph straighten out. Breaking it deliberately once is the fastest way to recognize the symptom in the wild.
