---
title: "Taming Metric Cardinality: Relabeling, Limits, and What a Runaway Label Really Costs"
date: 2026-08-15
track: observability
summary: "One innocent label — user_id, a raw URL path, a pod hash — turns a single metric into a million time series and OOM-kills your Prometheus. Here's how a series is counted, how to find the offender with /api/v1/status/tsdb, and how to stop the bleed with metric_relabel_configs and scrape limits. Current as of Prometheus 3.13."
reading_time: 5
tags: [prometheus, cardinality, relabeling, tsdb, metrics, promql]
sources:
  - title: "Prometheus — Configuration reference (metric_relabel_configs, sample_limit, label_limit)"
    url: "https://prometheus.io/docs/prometheus/latest/configuration/configuration/"
  - title: "Prometheus — HTTP API: TSDB stats (/api/v1/status/tsdb)"
    url: "https://prometheus.io/docs/prometheus/latest/querying/api/"
  - title: "Prometheus v3.13.0 release notes"
    url: "https://github.com/prometheus/prometheus/releases/tag/v3.13.0"
  - title: "Cardinality is key (Robust Perception)"
    url: "https://www.robustperception.io/cardinality-is-key/"
  - title: "High Cardinality in Prometheus: How to Find and Fix It (Last9)"
    url: "https://last9.io/blog/how-to-manage-high-cardinality-metrics-in-prometheus/"
---

A dashboard panel breaks, someone adds `user_id` to a counter to debug it, and three days later Prometheus is using 40 GB of RAM and getting OOM-killed on every restart. Nobody added a new metric. They added a *label* — and in a time-series database a label is a multiplier. This is the single most common way self-managed Prometheus (currently **3.13.2**, released 30 July 2026, the 3.13 LTS line) falls over.

## What a time series actually is

Prometheus doesn't store "metrics." It stores **time series**, and a series is a unique combination of a metric name plus every label key/value on it. `http_requests_total{method="GET", status="200", handler="/api"}` and `http_requests_total{method="GET", status="200", handler="/login"}` are two entirely separate series with two separate on-disk chunk streams and two entries in the in-memory index.

The count is a **Cartesian product**. Take a metric with 5 methods, 20 status codes, and 40 handlers: 5 × 20 × 40 = 4,000 series. All bounded, all fine. Now add `user_id` with 500,000 distinct values and the product explodes to two billion. Even a slice that actually appears in traffic can be millions of series.

The cost lands in the **head block** — Prometheus's in-memory window of the most recent, still-being-written samples. Every *active* series holds an index entry plus an open chunk it's appending to, so memory scales with the number of series you're ingesting, not with how many samples each one has. As Robust Perception puts it, cardinality is the thing that kills you: a label whose values are unbounded (a user ID, a raw URL with IDs baked in, an email, a full k8s pod name with its random hash) is a slow-motion outage. The rule of thumb: **a label value must come from a small, bounded set you could write down.**

## Finding the offender

Prometheus tells you exactly where the series went. The TSDB status endpoint returns the head-block breakdown:

```bash
curl -s http://localhost:9090/api/v1/status/tsdb | jq '.data'
```

Two fields matter: `seriesCountByMetricName` (which metric names own the most series) and `labelValueCountByLabelName` (which *labels* have the most distinct values — your prime suspects). The same view is in the web UI under **Status → TSDB Stats**.

Confirm with PromQL. Rank metrics by series count, then find which label is doing the damage:

```promql
# Top 10 metrics by number of series
topk(10, count by (__name__) ({__name__=~".+"}))

# For a suspect metric, how many distinct values does each label carry?
count(count by (user_id) (http_requests_total))
```

If that second query returns 500,000, you've found it.

## Fixing it: relabel, then limit

Two mechanisms, applied in this order.

**metric_relabel_configs** rewrites or drops samples *after* they're scraped but *before* they're stored — the right hook for trimming ingested metrics (distinct from `relabel_configs`, which acts on target labels *before* the scrape). Drop the runaway label outright with `labeldrop`:

```yaml
scrape_configs:
  - job_name: app
    static_configs:
      - targets: ["app:8080"]
    metric_relabel_configs:
      # 1. Delete unbounded labels entirely
      - regex: '(user_id|trace_id|session_id)'
        action: labeldrop

      # 2. Bucket a high-cardinality path into a template
      - source_labels: [path]
        regex: '/api/users/[0-9]+'
        target_label: path
        replacement: '/api/users/:id'

      # 3. Drop an entire noisy metric you never query
      - source_labels: [__name__]
        regex: 'go_gc_duration_seconds.*'
        action: drop
```

Rewriting (#2) preserves a useful, *bounded* version of the label; dropping (#3) removes a whole series family. Prefer `keep`/`labelkeep` allow-lists over chasing offenders one regex at a time when a target is chronically noisy.

**Scrape limits** are the guardrail for the label you *didn't* anticipate. Set on the scrape job, they make a runaway target fail loudly instead of quietly bloating the head block:

| Field | Enforces | Effect on breach |
|-------|----------|------------------|
| `sample_limit` | max series per scrape | whole scrape rejected, target marked down |
| `label_limit` | max labels per series | scrape rejected |
| `label_name_length_limit` | max label-name length | scrape rejected |
| `label_value_length_limit` | max label-value length | scrape rejected |
| `target_limit` | max targets per job | extra targets dropped |

```yaml
scrape_configs:
  - job_name: app
    sample_limit: 50000        # refuse a scrape that would add >50k series
    label_limit: 30
    label_value_length_limit: 512
    static_configs:
      - targets: ["app:8080"]
```

The honest caveat: `sample_limit` protects the *server* by sacrificing the *target* — when it trips you lose all metrics from that scrape, blinding yourself to the very instance that misbehaved. So it's a backstop, not a strategy. The durable fix is upstream: never put an unbounded value in a label in the first place. High-cardinality identifiers belong on **exemplars**, traces, or logs — not on a metric.

**Try next:** hit `/api/v1/status/tsdb` on your busiest Prometheus, read the top entry in `labelValueCountByLabelName`, and add a `sample_limit` to that job set to ~2× its current series count — then wait for the next offender to trip it instead of your pager.
