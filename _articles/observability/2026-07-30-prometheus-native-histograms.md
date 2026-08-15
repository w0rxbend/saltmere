---
title: "Native histograms: high-resolution latency without the bucket tax"
date: 2026-07-30
track: observability
summary: "Classic Prometheus histograms fix bucket boundaries at instrumentation time and cost one time series per boundary. Native histograms replace them with a single series of sparse exponential buckets generated from a schema integer. Instrumentation, ingestion, and PromQL are covered."
reading_time: 6
tags: [prometheus, histograms, promql, opentelemetry, cardinality]
sources:
  - title: "Native Histograms (Prometheus specification)"
    url: "https://prometheus.io/docs/specs/native_histograms/"
  - title: "client_golang: prometheus/histogram.go"
    url: "https://github.com/prometheus/client_golang/blob/main/prometheus/histogram.go"
  - title: "Prometheus v3.8.0 release notes"
    url: "https://github.com/prometheus/prometheus/releases/tag/v3.8.0"
  - title: "Histograms and summaries (Prometheus practices)"
    url: "https://prometheus.io/docs/practices/histograms/"
  - title: "Send native histograms with exponential buckets (Grafana Mimir)"
    url: "https://grafana.com/docs/mimir/latest/send/native-histograms/_exponential_buckets/"
---

**Gist.** A classic Prometheus histogram fixes its bucket boundaries at instrumentation time and materialises **one time series per boundary**, so accuracy and cardinality are directly coupled. A native histogram is a **single series carrying a sparse set of exponential buckets** whose boundaries are derived from a `schema` integer, giving *relative* rather than absolute resolution. The cost is that resolution is no longer fixed — the client library may reset the histogram, raise the zero threshold, or reduce the schema when its bucket limit is exceeded — and that every consumer in the storage and query path needs explicit support for the new sample type.

## The cost model of explicit buckets

A classic histogram named `http_request_duration_seconds` with 12 `le` boundaries is not one series but **12 `_bucket` series plus `_count` and `_sum`**, each replicated across every other label attached to the metric. The product of route, method, and status-code cardinality multiplies that base, so one histogram expands into thousands of series.

The accuracy purchased is limited. Boundaries are chosen before the distribution is measured. `histogram_quantile` **interpolates linearly within the bucket the quantile falls into**, so a p99 landing inside a bucket spanning 1s–2.5s is bounded only by that bucket's width. When latency shifts, the interesting observations accumulate in the top bucket, which is the widest — resolution is lowest precisely where the distribution has moved.

The resulting trade-off is permanent: more boundaries for accuracy, fewer boundaries for cost.

## Bucket generation from the schema

A native histogram stores a *sparse* set of exponential buckets. Boundaries are not enumerated by the instrumenting code; they are generated from a `schema` integer. For a standard schema *n*, the positive bucket boundaries are

```
(2 ** (2 ** -n)) ** i        # i is the bucket index
```

so schema *n* has **exactly half the resolution of schema *n*+1**. Standard exponential schemas run from **-4 to 8**. Schema **-53** is reserved for custom (explicit) boundaries stored in the native format.

Two properties follow from this construction. First, resolution is **relative**: bucket width grows proportionally with the observed value, so a bucket near 20 ms is narrow in absolute terms and a bucket near 20 s is wide, and the relative error is uniform across the range. Second, the representation is **sparse** — only populated buckets are stored. A distribution clustered between 20 ms and 300 ms occupies on the order of a dozen buckets rather than the schema's full index range, and it remains **one series regardless of schema**, so raising resolution does not multiply cardinality.

Resolution is selected through a **bucket growth factor** rather than the raw schema:

| Factor | Schema | Meaning |
|--------|--------|---------|
| 1.1    | 3      | each power of two split into 8 buckets |
| 1.05   | 4      | ~16 buckets per power of two |
| 1.01   | 7      | ~128 buckets per power of two |

Factor **1.1 (schema 3)** is the documented reference point: roughly **10% relative error per bucket**, a small fixed bucket count, and a single series.

## Instrumentation in Go

In `client_golang`, native-histogram mode is enabled by setting `NativeHistogramBucketFactor`. The `Buckets` field may be omitted entirely, since the exponential scheme supersedes explicit boundaries:

```go
histogram := prometheus.NewHistogramVec(
    prometheus.HistogramOpts{
        Name: "http_request_duration_seconds",
        Help: "Request latency.",
        // Enables native-histogram mode. 1.1 -> schema 3.
        NativeHistogramBucketFactor: 1.1,
        // Bounds live buckets; on overflow the client resets, raises the
        // zero threshold, or halves resolution rather than growing on.
        NativeHistogramMaxBucketNumber:  160,
        NativeHistogramMinResetDuration: time.Hour,
    },
    []string{"route", "method", "code"},
)
```

`NativeHistogramMaxBucketNumber` bounds memory. When a distribution populates more buckets than the limit, the client library applies three steps in order: **if `NativeHistogramMinResetDuration` has elapsed since the last reset or creation, the whole histogram is reset**; otherwise **the zero threshold is raised** far enough to bring the bucket count back to the limit, but no further than `NativeHistogramMaxZeroThreshold`; and if the count still exceeds the limit, **the resolution is reduced by doubling the bucket width**. Observations in the closed interval [−threshold, +threshold] around zero, where the default `NativeHistogramZeroThreshold` is **2^-128**, are collapsed into a single zero bucket.

Native histograms are carried in Prometheus's **protobuf exposition format**, which was extended for them; OpenMetrics has no native-histogram support at the time of writing. The scraper requests protobuf through content negotiation, so the `/metrics` handler is unchanged.

## Ingestion

Native histograms were introduced as an experimental feature in **v2.40.0 (November 2022)** and remained experimental across the [Prometheus 3.0](/articles/observability/2026-07-25-prometheus-3-whats-new) line up to v3.7, gated behind `--enable-feature=native-histograms`. As of **v3.8.0 (December 2025)** they are a stable, opt-in feature controlled by the `scrape_native_histograms` setting, available globally and per scrape configuration:

```yaml
# prometheus.yml
scrape_configs:
  - job_name: my-app
    scrape_native_histograms: true   # stable, v3.8+
    static_configs:
      - targets: ["app:8080"]
```

The `--enable-feature=native-histograms` flag still functions in v3.8, where it flips the default of `scrape_native_histograms` to true, and becomes a complete no-op in v3.9. From v4 onward the spec records that native-histogram scraping is enabled by default.

## Querying

Because the buckets are contained within one series, **there is no `le` label to aggregate over**. The p99 is:

```promql
histogram_quantile(
  0.99,
  sum by (route) (rate(http_request_duration_seconds[5m]))
)
```

`rate()` and `sum()` operate directly on native-histogram samples and **return a native histogram**, which `histogram_quantile` then consumes. Companion functions read the same series:

```promql
# request rate straight from the histogram (no separate _count series)
histogram_count(rate(http_request_duration_seconds[5m]))

# mean latency = sum / count
histogram_sum(rate(http_request_duration_seconds[5m]))
  / histogram_count(rate(http_request_duration_seconds[5m]))

# fraction of requests served under 300ms (an SLI, directly)
histogram_fraction(0, 0.3, rate(http_request_duration_seconds[5m]))
```

`histogram_fraction(lower, upper, v)` has no clean classic equivalent unless an `le` boundary coincides exactly with the service-level-objective (SLO) threshold; with generated boundaries the threshold is arbitrary.

## OpenTelemetry interoperability

An OpenTelemetry (OTel) **exponential histogram** and a Prometheus native histogram share a representation: OTel's `scale` corresponds to Prometheus's `schema`, the zero bucket corresponds to OTel's zero count, and bucket indices differ by a constant offset. Prometheus's native OpenTelemetry Protocol (OTLP) receiver converts OTel exponential histograms into native histograms on ingest, so OTel-instrumented and `client_golang`-instrumented services arrive in the same form without explicit boundaries on either side.

## Residual costs

Long-term-storage backends and query tooling require explicit support for the native sample type; Grafana Mimir documents ingesting native histograms with exponential buckets, but support in any other component of a given deployment must be checked rather than assumed. Recording rules and dashboards written against `_bucket` and `le` do not carry over and require rewriting. Because a series can change resolution at runtime, **alerting on exact bucket boundaries is not stable**. What is exchanged is a fixed pre-measurement guess and per-boundary billing for one self-scaling series with uniform relative error.

## Pitfalls

- **Aggregating by `le` returns nothing.** Native histograms carry no `le` label, so a recording rule of the form `sum by (le) (rate(...))` copied from a classic setup yields an empty result rather than an error.
- **An alert threshold pinned to a bucket edge drifts.** When the client library reduces the schema on overflow, boundary positions change, so a rule that depends on a specific boundary silently begins measuring a different interval.
- **A pathological distribution triggers resolution loss, not an error.** Exceeding `NativeHistogramMaxBucketNumber` causes a reset, a raised zero threshold, or a halved resolution; quantiles become coarser or the counts restart, with no failure signal in the metric itself.
- **Sub-threshold observations disappear into the zero bucket.** Values at or below `NativeHistogramZeroThreshold` are indistinguishable from one another, so quantiles in the very low range are bounded by that threshold.
- **Scraping without `scrape_native_histograms` discards the buckets.** A target instrumented with `NativeHistogramBucketFactor` but scraped by a server that has not enabled ingestion yields only classic output, and the resolution work is invisible.
- **An unsupported downstream store drops the samples.** A remote-write receiver or query layer without native-histogram support loses the series rather than degrading it to classic buckets.
