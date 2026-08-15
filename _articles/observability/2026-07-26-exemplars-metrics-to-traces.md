---
title: "Exemplars: the sticky note a metric leaves for a trace"
date: 2026-07-26
summary: "A histogram reports that the 99th percentile regressed but not which request caused it. Exemplars attach a sampled trace identifier to a metric sample so a dashboard can link from the graph to the trace. This covers the exposition format, the storage flag, and the wiring."
track: observability
reading_time: 6
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

**Gist.** A latency histogram is an aggregate: thousands of observations folded into bucket counts, with the identity of each observation discarded by construction, so it can report that a percentile regressed but not which request regressed it. An exemplar restores one link by attaching a small label set — conventionally a trace identifier (`trace_id`) — plus the observed value to the bucket the observation landed in, letting a dashboard deep-link from a point on the graph into the corresponding distributed trace. The cost is that the link is sampled and bounded: **at most one exemplar per bucket sample**, stored in a fixed-size in-memory buffer that is opt-in, so the connection is best-effort and every stage in the pipeline fails to an empty state rather than an error.

## What an exemplar is

An exemplar is a single sampled data point attached to a metric sample: a set of labels, the exact value observed, and optionally a timestamp. It is not a summary. It is one specific measurement retained alongside the bucket it fell into.

The [OpenMetrics specification](https://github.com/prometheus/OpenMetrics/blob/main/specification/OpenMetrics.md) defines the syntax. On a histogram, an exemplar rides on the bucket line, after a `#` separator:

```
http_request_duration_seconds_bucket{le="0.5"} 129389 # {trace_id="a1b2c3d4e5f6"} 0.372 1690000000.928
```

The specification constrains this construct in three ways that determine how it behaves in practice:

- The exemplar's **label set is capped at 128 UTF-8 characters combined** (names and values together). A trace identifier fits; an attempt to carry a request body or a stack fragment does not.
- The **sample value must fall inside the bucket's range**. An exemplar on the `le="0.5"` bucket asserts an observation at or below 0.5 seconds.
- Each bucket line carries **at most one exemplar**. A client library retains one observation per bucket — the most recent, or one chosen by sampling — and discards the rest.

Exemplars are legal only on Histogram bucket samples, GaugeHistogram bucket samples, and Counter totals. They cannot be attached to an arbitrary Gauge.

## The invariant the pipeline must preserve

Four independent components must each cooperate, and the identifier must survive unchanged through all of them. The label emitted by the client library, serialized on the `/metrics` endpoint, parsed and stored by Prometheus, and named in the dashboard's data-source configuration must be **the same label name and the same identifier value**. Any stage that drops it breaks the link without reporting a fault.

| Stage | Component | Responsibility |
|---|---|---|
| Emit | Application plus OpenTelemetry (OTel) or Prometheus client library | Attach the active `trace_id` to the histogram observation |
| Expose | `/metrics` endpoint | Serialize the exemplar in OpenMetrics exposition format |
| Scrape and store | Prometheus | Parse the exemplar and persist it (opt-in) |
| Render | Grafana | Draw the exemplar as a marker and deep-link to the trace |

**Emit.** A Prometheus client library exposes exemplar support through an `ObserveWithExemplar`-style call — in Go, the histogram's `ExemplarObserver` interface. Inside a traced handler, the current span's trace identifier is read from the context and passed as an exemplar label:

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

OpenTelemetry SDKs perform the equivalent attachment for metrics recorded inside an active span context, keeping the sampling decision inside the SDK rather than in application code.

**Expose.** The endpoint serializes exemplars only when the scrape negotiates the OpenMetrics text format via `Accept: application/openmetrics-text`. **The legacy Prometheus text exposition format has no representation for exemplars at all**, so a manual `curl` that does not send the header retrieves a valid but exemplar-free response. Prometheus requests OpenMetrics itself.

**Scrape and store.** Exemplar storage is an opt-in feature flag rather than a default:

```
--enable-feature=exemplar-storage
```

Per the [Prometheus feature flags documentation](https://prometheus.io/docs/prometheus/latest/feature_flags/), exemplar storage is a **fixed-size in-memory circular buffer**, so exemplars are retained only until overwritten, and a single exemplar carrying only a `trace_id` costs roughly 100 bytes. The buffer size is set in the configuration file:

```yaml
storage:
  exemplars:
    max_exemplars: 100000
```

Without the flag, Prometheus scrapes and stores the histogram normally and drops every exemplar line it parses, with no warning emitted. The resulting series is indistinguishable from one that was never instrumented.

**Render.** In Grafana, exemplar support is configured on the Prometheus data source. The [Grafana documentation](https://grafana.com/docs/grafana/latest/fundamentals/exemplars/) describes them rendering as marker points overlaid on the time series, with a hover panel showing the trace identifier and a button that opens the configured trace data source. The deep link requires the data source's exemplars configuration to map a label to a destination:

```yaml
exemplarTraceIdDestinations:
  - datasourceUid: tempo-uid
    name: trace_id
```

`name` must equal the label name the client library used, and `datasourceUid` identifies the Tempo or Jaeger data source. A mismatched `name` still renders the markers; the link resolves to nothing.

### Implementation sketch (Scala)

The retention rule — one exemplar per bucket, overwritten by later observations in the same bucket — is the load-bearing mechanism. A minimal histogram implementing it:

```scala
final case class Exemplar(labels: Map[String, String], value: Double, ts: Double)

final class ExemplarHistogram(bounds: Vector[Double]):
  // bounds are the upper inclusive edges (`le`); the final +Inf bucket is implicit.
  private val counts    = Array.fill(bounds.length + 1)(0L)
  private val exemplars = Array.fill[Option[Exemplar]](bounds.length + 1)(None)

  private def bucketOf(v: Double): Int =
    val i = bounds.indexWhere(v <= _)
    if i < 0 then bounds.length else i

  def observe(v: Double, labels: Map[String, String], ts: Double): Unit =
    val i = bucketOf(v)
    counts.indices.drop(i).foreach(j => counts(j) += 1) // cumulative buckets
    if labelBudget(labels) <= 128 then
      exemplars(i) = Some(Exemplar(labels, v, ts)) // last writer per bucket wins

  private def labelBudget(labels: Map[String, String]): Int =
    labels.foldLeft(0)((n, kv) => n + kv._1.length + kv._2.length)

  def expose(name: String): String =
    (bounds.map(_.toString) :+ "+Inf").zipWithIndex.map { case (le, i) =>
      val ex = exemplars(i).fold("") { e =>
        val ls = e.labels.map((k, v) => s"""$k="$v"""").mkString(",")
        s" # {$ls} ${e.value} ${e.ts}"
      }
      s"""${name}_bucket{le="$le"} ${counts(i)}$ex"""
    }.mkString("\n")
```

Two properties follow directly from the array layout. An observation increments every cumulative bucket at or above its own index but writes its exemplar to **exactly one** slot, so a rare slow request leaves its receipt only in the bucket that first contains it. And because the write is an unconditional overwrite, the exemplar surviving a scrape interval is the last qualifying observation in that bucket, not the slowest one.

## Pitfalls

- Prometheus started without `--enable-feature=exemplar-storage` parses exemplar lines and discards them silently; the symptom is a correctly populated histogram with no markers and no log entry naming exemplars.
- Fetching `/metrics` with a plain `curl` shows no exemplars because the default request does not negotiate `application/openmetrics-text`, and the legacy format cannot encode them; the endpoint is not at fault.
- A label set exceeding **128 UTF-8 characters** violates the OpenMetrics constraint, so attaching span attributes alongside `trace_id` can push the exemplar past the cap.
- Naming the label `traceID` in the client library while `exemplarTraceIdDestinations.name` says `trace_id` renders markers whose link target is empty — the failure appears in the click, not in the graph.
- The exemplar buffer is fixed-size and in-memory: at high observation rates the exemplar for an old spike is overwritten before an investigation reaches it, and a Prometheus restart empties it entirely.
- An exemplar identifies *an* observation in the bucket, not the worst one; treating the linked trace as the p99 outlier is unsound when the bucket is wide.
