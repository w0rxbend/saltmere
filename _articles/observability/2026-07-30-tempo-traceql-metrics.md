---
title: "TraceQL metrics: computing rates and latencies from raw traces in Tempo"
date: 2026-07-30
track: observability
summary: "Trace stores traditionally support two operations: fetch a trace by identifier, or search for traces matching a filter. TraceQL metrics adds a third — PromQL-style aggregation evaluated at query time over the raw spans — and pays for it by scanning span data on every query."
reading_time: 6
tags: [observability, tracing, tempo, traceql, metrics, grafana]
sources:
  - title: "TraceQL — Grafana Tempo documentation"
    url: "https://grafana.com/docs/tempo/latest/traceql/"
  - title: "Metrics from traces / TraceQL metrics — Grafana Tempo documentation"
    url: "https://grafana.com/docs/tempo/latest/metrics-from-traces/metrics-queries/"
  - title: "Grafana Tempo 2.9 release: MCP server support, TraceQL metrics sampling, and more — Grafana Labs (Oct 2025)"
    url: "https://grafana.com/blog/2025/10/22/grafana-tempo-2-9-release-mcp-server-support-traceql-metrics-sampling-and-more/"
  - title: "Powerful new language features for TraceQL — Grafana Labs (May 2025)"
    url: "https://grafana.com/whats-new/2025-05-08-powerful-new-language-features-for-traceql/"
---

**Gist.** A trace store holds millions of structured, timestamped, attributed spans, but the classic access paths — lookup by trace identifier, and filter-then-open-one-trace — expose none of that structure to aggregation. TraceQL metrics, a feature of Grafana Tempo (**2.9**, released October 2025), adds a pipe operator that turns a matched span set into a time series using Prometheus-shaped functions, so an aggregation can be defined **after** the data was collected rather than before. The cost is that every such query scans span data instead of reading a pre-computed counter, which is why Tempo 2.9 also ships a sampling modifier that trades accuracy for scan volume.

## The selection half of TraceQL

TraceQL (Trace Query Language) is first a span-selection language. A brace-delimited expression is a boolean predicate over the attributes of a single span, with scopes distinguishing span-level attributes from the attributes of the resource that emitted the span:

```
{ span.http.status_code >= 500 && resource.service.name = "checkout" }
```

The result is the set of traces containing at least one span satisfying the predicate. **The unit of matching is the span; the unit of the classic result is the trace** — a distinction that becomes load-bearing once aggregation enters, because aggregation operates on the matched spans, not on the traces they belong to.

TraceQL additionally provides *structural* operators, which relate two span sets by their position in the trace tree rather than by their attributes:

```
{ resource.service.name = "gateway" } >> { span.db.system = "postgres" }
```

`>>` is the descendant operator: it selects gateway spans having a Postgres span somewhere beneath them in the trace tree. This is a query over the **shape** of the trace. Tag-indexed trace search cannot express it, because a flat tag index records which tags a trace carries, not how the spans carrying them are nested.

## The aggregation half

TraceQL metrics introduces a pipe (`|`) that consumes a span set and produces a time series. The function names mirror Prometheus:

```
# Rate of failing checkout spans, broken down by route.
{ span.http.status_code >= 500 && resource.service.name = "checkout" }
  | rate() by (span.http.route)
```

```
# p95 latency of database spans over time.
{ span.db.system = "postgres" } | quantile_over_time(duration, 0.95)
```

```
# Span throughput per service.
{ } | count_over_time() by (resource.service.name)
```

`rate()`, `count_over_time()`, `quantile_over_time()` and `histogram_over_time()` all consume the spans the filter selected and emit series that render in the same Grafana panels, under the same time controls, as Prometheus series.

The `by (...)` clause groups on a span attribute. **The grouping key is chosen at query time, so it is not constrained by the label set decided at instrumentation time.** That is the substantive difference from a pre-aggregated metric such as `http_request_duration_seconds`. A pre-aggregated metric must fix its labels before collection; each additional label multiplies the number of distinct series the metrics backend stores and indexes, and that product is what makes high-cardinality labels — route, status, customer tier — expensive to add speculatively. TraceQL metrics computes the aggregation from raw spans on demand, so a dimension that was never a metric label is still available, including for traffic that has already been recorded and retained.

The corresponding limitation follows from the same mechanism: **an attribute that was never attached to a span cannot be recovered by any query**, and a period outside the trace retention window cannot be aggregated at all. Query-time aggregation removes the need to predict the *label set*; it does not remove the need to instrument the attribute or to retain the spans.

## The scan cost, and the two mitigations in 2.9

Evaluating an aggregation over raw spans means reading span data for the whole query window. Two mechanisms bound that work.

The first is storage layout. Tempo stores traces in a columnar Parquet format (**vParquet**). In a columnar layout, values of one field are stored contiguously, so a query referencing only `duration` and `resource.service.name` reads those columns and skips the rest of the span payload. The volume read scales with the number of *referenced fields*, not with the full width of a span — which is what makes scan-based aggregation viable rather than merely possible.

The second is **TraceQL metrics sampling**, added in Tempo 2.9 and enabled per query with the `with (sample=...)` modifier — `sample=true` for dynamic sampling, or a fixed fraction such as `sample=0.01` to inspect one per cent of the data:

```
{ span.db.system = "postgres" }
  | quantile_over_time(duration, 0.95) with (sample=true)
```

The aggregation is computed over a subset of the matching spans and the result extrapolated. Grafana's release post reports one query dropping from **7.35 s to 2.89 s** under sampling. The accuracy loss is not uniform across functions: an extrapolated count or rate is an estimate whose error depends on how many spans the sample contained, and a quantile estimated from a subset is sensitive to the tails that sampling is most likely to thin out. Sampling suits queries where the trend's shape is the object of interest; it is the wrong setting for a query whose absolute value is being compared against a threshold.

Tempo 2.9 also ships an **MCP (Model Context Protocol) server**, documented as experimental, exposing trace data so that an assistant can issue TraceQL queries directly rather than having them hand-written.

## Position among the observability signals

The conventional division — metrics answer "is something wrong", traces answer "where" — is a statement about which store can aggregate, not about the data itself. TraceQL metrics moves aggregate questions into the trace store for the dimensions that were not pre-planned. A Prometheus deployment remains the cheaper answer for always-on RED (rate, errors, duration) and USE (utilisation, saturation, errors) dashboards, because those read pre-computed series at fixed low cardinality and do not scan span data per refresh. The two are complementary along a single axis: **pre-aggregation pays storage and cardinality cost once per label set; query-time aggregation pays scan cost once per query.**

No published benchmark separates the two on a common workload, so the choice is properly made on which cost the deployment can absorb, not on a measured crossover point.

## Pitfalls

- **Placing a high-cardinality attribute in `by (...)` on a wide time range produces both a slow query and an unreadable panel.** The grouping is evaluated during the scan, so the series count is bounded only by the distinct values present in the scanned spans, not by anything decided at instrumentation time.
- **A dashboard panel refreshing on a short interval re-runs the full scan each time.** Unlike a Prometheus query, there is no pre-aggregated series to read; the cost recurs with every refresh and with every viewer.
- **`with (sample=true)` left on a panel used for alerting or capacity comparison reports extrapolated values.** The number displayed is an estimate derived from a subset, and its error is not surfaced in the panel.
- **Quantiles under sampling are least reliable exactly where they matter.** High quantiles are determined by the slowest spans, which are the least numerous and therefore the most likely to be thinned out of the sample.
- **Filtering selects traces but aggregation consumes spans.** A predicate matching one span in a trace makes that trace a search result, while `rate()` and `count_over_time()` count the matched spans; reading a span rate as a request rate overcounts whenever a trace contains several matching spans.
- **An attribute absent from the emitted spans cannot be added retroactively.** Query-time aggregation frees the choice of grouping key, not the choice of what was instrumented.
