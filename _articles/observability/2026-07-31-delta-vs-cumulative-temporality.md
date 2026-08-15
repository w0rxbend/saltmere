---
title: "Delta vs cumulative: the OpenTelemetry temporality setting that silently corrupts dashboards"
date: 2026-07-31
track: observability
summary: "OTLP metrics carry an aggregation temporality — cumulative (total since start) or delta (change since the last export). A mismatch between producer and backend yields wrong rates with no error raised anywhere. This article covers how the two encodings differ, how to choose, and how to convert in the Collector."
reading_time: 6
tags: [opentelemetry, metrics, prometheus, otlp, temporality]
sources:
  - title: "OpenTelemetry metrics: Delta vs. Cumulative temporality trade-offs — Grafana Labs"
    url: "https://grafana.com/blog/opentelemetry-metrics-a-guide-to-delta-vs-cumulative-temporality-trade-offs/"
  - title: "Producing Delta Temporality Metrics with OpenTelemetry — Datadog docs"
    url: "https://docs.datadoghq.com/opentelemetry/guide/otlp_delta_temporality/"
  - title: "OpenTelemetry Metrics Data Model — aggregation temporality (spec)"
    url: "https://opentelemetry.io/docs/specs/otel/metrics/data-model/"
---

**Gist.** An OpenTelemetry Protocol (OTLP) sum or histogram carries a flag called **aggregation temporality**, declaring whether each reported number is a running total or an increment covering one export interval. Backends interpret that number according to what they expect rather than what was sent, so a producer/backend mismatch produces plausible-looking, arithmetically wrong graphs — a saw-tooth where a rising line belongs, or a flat `rate()` under demonstrably varying traffic. The cost of resolving it is a choice between client-side memory (cumulative retains per-series state for the process lifetime) and backend-side reconstruction (delta makes every dropped export a permanently lost increment).

## The two encodings

The same measurement can be transmitted two ways.

- **Cumulative:** the reported value is the total accumulated since the process, or the metric, started. A request counter reading 1,000 means one thousand observations since startup. The series is monotonically non-decreasing until a restart returns it to zero. This is **Prometheus's native model**: `rate()` and `increase()` are defined over an ever-rising line, computing the slope between samples, and they **detect the drop to zero as a counter reset** rather than as a negative rate.
- **Delta:** the reported value is the change *since the previous export*. The same counter reports 37 for one interval, then 52 for the next; the numbers do not accumulate, and each export stands alone. This is the shape **StatsD- and Datadog-style** systems expect, because summation happens server-side.

Neither encoding is more correct in the abstract. They carry the same information under different conventions about who holds the accumulator.

## The failure mode

The flag is metadata that the backend is entitled to trust. Nothing in the pipeline compares the flag against the receiving system's expectation, so the mismatch never surfaces as an error — only as arithmetic.

**Delta series into a cumulative-expecting backend.** Consecutive samples are 37, 52, 41, 60. To a system reading these as running totals, 52 → 41 is a decrease, which its counter semantics interpret as **a reset on that scrape**. Because delta values fluctuate freely, resets appear at an arbitrary fraction of intervals, and rate computations mix genuine slopes with reset-recovery arithmetic. The resulting graph is a saw-tooth pinned near the magnitude of a single interval instead of a curve tracking a growing total.

**Cumulative series into a delta pipeline.** Consecutive samples are 1000, 1037, 1089. A backend that sums what it receives adds already-summed values, so the reported total grows roughly as the *integral* of the true total. Every interval's contribution is inflated by the entire history preceding it.

Both cases produce a chart that renders, refreshes, and carries no warning.

## Selecting temporality to match the destination

The selection rule is determined by the consumer, not by the instrument.

- Prometheus, Mimir, Thanos, and anything else speaking PromQL → **cumulative**. This is the default in the OpenTelemetry Software Development Kits (SDKs).
- Datadog and other delta-native backends → **delta**, which such backends often require for counters to be interpreted correctly.

The preference is configured once at the exporter, by environment variable:

```bash
# cumulative (Prometheus-friendly) — usually the default
export OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative
# or delta, for a delta-native backend
export OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=delta
```

A third value, `lowmemory`, mixes the two: the specification assigns delta to synchronous counters and histograms, and cumulative to the remaining instrument kinds.

## The resource trade behind the flag

The setting is not purely a compatibility switch; the two encodings impose different costs on different components.

**Cumulative** obliges the SDK to hold a running total **for every series, for the life of the process**. Memory therefore scales with the cardinality of the attribute sets observed and is never reclaimed while the process lives. A restart presents as a reset that the backend must recognise and compensate for.

**Delta** allows the client to discard each interval's accumulator immediately after export. The footprint is bounded by the series seen within one interval rather than over the process lifetime, which suits **ephemeral executions such as serverless functions that terminate between requests** and never accumulate a long-lived total. The cost moves to the backend, which must perform the summation and must cope with exports arriving late or duplicated. Critically, **a delta export lost in transit is a permanently lost increment**: nothing in a later export restates it. Under cumulative encoding, the next successful export restores the correct total, so a dropped payload costs resolution rather than accuracy.

## Converting when the producer cannot be changed

A third-party service may emit delta into an environment running Prometheus. The conversion belongs **in the OpenTelemetry Collector**, not at the source:

```yaml
processors:
  deltatocumulative:            # delta in -> cumulative out (for Prometheus)
    max_stale: 5m
service:
  pipelines:
    metrics:
      processors: [deltatocumulative]
```

A `cumulativetodelta` processor performs the reverse direction. Placing the conversion in the Collector confines the fix to one configuration rather than distributing it across every service's SDK configuration.

The `max_stale` setting bounds how long the processor retains the accumulator for a series that has stopped reporting. Retention is what makes the conversion stateful: reconstructing a total requires remembering the previous total for every series in flight.

### Implementation sketch (Scala)

The accumulator underlying a delta-to-cumulative conversion, showing the state that `max_stale` bounds and the reset rule that makes the output a valid cumulative series.

```scala
final case class Key(name: String, attrs: Map[String, String])
final case class Acc(total: Double, startNanos: Long, lastSeenNanos: Long)

final class DeltaToCumulative(maxStaleNanos: Long):
  private var state: Map[Key, Acc] = Map.empty

  /** Returns the cumulative value and the start timestamp it is measured from. */
  def accumulate(k: Key, delta: Double, startNanos: Long, nowNanos: Long)
      : (Double, Long) =
    state.get(k) match
      case Some(a) if nowNanos - a.lastSeenNanos > maxStaleNanos =>
        // Gap exceeds retention: the prior total is no longer trustworthy,
        // so a fresh accumulation begins at this point's start time.
        emit(k, Acc(delta, startNanos, nowNanos))
      case Some(a) if startNanos > a.startNanos =>
        emit(k, Acc(delta, startNanos, nowNanos))   // producer restarted
      case Some(a) =>
        emit(k, a.copy(total = a.total + delta, lastSeenNanos = nowNanos))
      case None =>
        emit(k, Acc(delta, startNanos, nowNanos))

  private def emit(k: Key, a: Acc): (Double, Long) =
    state = state.updated(k, a)
    (a.total, a.startNanos)

  def evict(nowNanos: Long): Unit =
    state = state.filter((_, a) => nowNanos - a.lastSeenNanos <= maxStaleNanos)
```

The invariant is that **the emitted total is non-decreasing for as long as the start timestamp is unchanged**; any increase in the start timestamp signals a new accumulation and is the signal a PromQL reader treats as a counter reset.

## Pitfalls

- **A temporality mismatch raises no error.** The symptom is a saw-tooth counter or a flat `rate()` under varying traffic; the cause is that the backend applies its own convention to the received value without consulting the flag against any expectation.
- **Cumulative memory grows with attribute cardinality, not with traffic.** A process whose resident set climbs steadily while request volume is flat is holding one running total per distinct attribute combination, retained for the process lifetime.
- **A dropped delta export is an unrecoverable gap.** The increment appears in no later payload, so the total is permanently understated; the equivalent drop under cumulative encoding self-heals at the next successful export.
- **Setting temporality per service leaves the conversion scattered.** A pipeline mixing producers configured independently will present some series correctly and others not, with no single place to inspect; the Collector processor concentrates it.
- **`max_stale` set too short breaks long-interval series.** A series exporting less often than the retention window has its accumulator evicted between exports, so each arriving delta starts a new accumulation and the output resets repeatedly.
- **Restarting a cumulative producer looks like data loss until the reset is recognised.** PromQL handles the drop to zero; a consumer without counter-reset semantics reports a large negative rate.
