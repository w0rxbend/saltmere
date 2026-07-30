---
title: "TraceQL metrics: computing rates and latencies from raw traces in Tempo"
date: 2026-07-30
track: observability
summary: "Traces used to be for one thing: find the slow request and stare at its span tree. TraceQL metrics flips that — you run PromQL-like aggregations directly over your trace data, so 'p95 latency of failing checkout spans, by route' is one query against the traces you already store. Here's the query model and how it fits the pillars."
reading_time: 5
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

Distributed tracing has a storage paradox: you keep millions of spans, but the tooling only lets you do two things with them — look up a trace by ID, or search for traces matching a filter and open one. All that structured, timed, attributed data, and you can't *aggregate* over it. TraceQL metrics, now a core feature of Grafana Tempo (latest release **2.9**, October 2025), fixes that. It lets you run PromQL-style aggregations directly against raw trace data, so your traces become a metrics source without a separate pipeline.

## First, the filter half of TraceQL

TraceQL's base is a span-selection language. You match spans with `{ }` and boolean conditions over span attributes:

```
{ span.http.status_code >= 500 && resource.service.name = "checkout" }
```

That returns traces containing a checkout span that failed. TraceQL also has *structural* operators — the killer feature over old tag-based trace search — that let you express parent/child and descendant relationships between spans:

```
{ resource.service.name = "gateway" } >> { span.db.system = "postgres" }
```

The `>>` means "a gateway span that has a Postgres span somewhere in its descendants." You're querying the *shape* of the trace, not just individual span tags — "find requests that hit the gateway and eventually touched the database" is one line.

## The metrics half: pipe spans into an aggregation

TraceQL metrics adds a pipe (`|`) that turns a set of matched spans into a time series. The functions mirror Prometheus:

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
# Count of spans per service — a service-level throughput view.
{ } | count_over_time() by (resource.service.name)
```

`rate()`, `count_over_time()`, `quantile_over_time()`, `histogram_over_time()`, and friends all operate on the spans your filter selected, producing series you graph exactly like Prometheus metrics — in the same Grafana panels, with the same time controls.

Why is this different from just recording a metric in the first place? **Cardinality and hindsight.** A pre-aggregated metric like `http_request_duration_seconds` had to decide its label set *before* the data was collected — add `route` and `status` and `customer_tier` and you get a cardinality explosion your metrics backend can't afford. TraceQL metrics computes the aggregation *at query time* from the raw spans, so you can slice by any span attribute you like, including ones you never thought to make a metric label — and you can ask questions about *last week's* traffic that you didn't instrument for. It's the "explore an arbitrary dimension after the fact" superpower that fixed-schema metrics can't give you.

## The honest cost, and how Tempo 2.9 addresses it

Computing metrics over raw spans at query time means scanning a lot of data — that's the tradeoff for the flexibility. Tempo stores traces in a columnar Parquet format (**vParquet**), which is what makes these scans feasible at all, since a query touching only `duration` and `service.name` reads just those columns. Tempo 2.9 added **TraceQL metrics sampling** (`with (sample=true)`) precisely for this: it computes the aggregation over a sampled subset and extrapolates, trading a little accuracy for a large speedup — Grafana's own example showed a query dropping from 7.35s to 2.89s. For exploratory dashboards where you want the *shape* of a trend, that's the right trade.

That release also shipped an **MCP server** — Tempo can expose its trace data to LLM assistants over the Model Context Protocol, so an AI agent can issue TraceQL queries directly instead of you hand-writing them. Whatever you think of that, it signals where trace *querying* is going: traces as a first-class, queryable data source, not a last-resort debugging view.

## Where it sits among the pillars

The tidy mental model — metrics for "is something wrong," traces for "where" — gets blurrier in a good way here. TraceQL metrics lets your *trace* store answer aggregate "is something wrong, and along which dimension" questions without a parallel metrics pipeline for every breakdown you might want. You still keep Prometheus for cheap, always-on RED/USE dashboards (those don't need per-request detail), but the moment you want to aggregate along a high-cardinality dimension you didn't pre-plan, TraceQL metrics answers it from data you were already storing.

**Try next:** In a Grafana with a Tempo data source, take a service you already trace and run `{ resource.service.name = "yours" } | quantile_over_time(duration, 0.95) by (span.http.route)` — a per-route p95 you never had to instrument as a metric — then add `with (sample=true)` on a wide time range and compare both the latency of the query and how much the numbers move. That difference is the accuracy you're trading for speed.
