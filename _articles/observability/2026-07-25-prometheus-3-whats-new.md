---
title: "Prometheus 3.0: what actually changed, and what it breaks"
date: 2026-07-25
track: observability
summary: "The first major Prometheus release in seven years ships a native OTLP endpoint, UTF-8 names, and a rewritten UI — plus a quiet PromQL change that can silently drop samples. Here's what to touch before you upgrade."
reading_time: 5
tags: [prometheus, otlp, promql, remote-write]
sources:
  - title: "Announcing Prometheus 3.0"
    url: "https://prometheus.io/blog/2024/11/14/prometheus-3-0/"
  - title: "Prometheus 3.0 migration guide"
    url: "https://prometheus.io/docs/prometheus/latest/migration/"
  - title: "Using Prometheus as your OpenTelemetry backend"
    url: "https://prometheus.io/docs/guides/opentelemetry/"
  - title: "Prometheus v3.0.0 release notes (GitHub)"
    url: "https://github.com/prometheus/prometheus/releases/tag/v3.0.0"
  - title: "Grafana: Prometheus 3.0 and OpenTelemetry, a practical guide"
    url: "https://grafana.com/blog/prometheus-3-0-and-opentelemetry-a-practical-guide-to-storing-and-querying-otel-data/"
---

Prometheus 3.0 landed on **14 November 2024** — the first major bump since 2.0 in 2017. Most of the headline items are additive (a new UI, an OTLP receiver, UTF-8 names), so it's tempting to treat it as a routine upgrade. Don't: one PromQL change alters query results, and a pile of long-standing feature flags now error on startup. Here's the operator's-eye view.

## Prometheus can now receive OTLP directly

The change that matters most if you run OpenTelemetry: Prometheus can ingest OTLP metrics natively, no `prometheusreceiver` in a Collector required. It's off by default. Turn it on with a flag and a small config block:

```bash
prometheus --web.enable-otlp-receiver \
  --config.file=/etc/prometheus/prometheus.yml
```

```yaml
# prometheus.yml
otlp:
  # keep dots in names instead of rewriting them to underscores
  translation_strategy: NoTranslation
  promote_resource_attributes:
    - service.name
    - service.namespace
    - service.instance.id
    - k8s.namespace.name
    - k8s.pod.name
```

Point any OTLP/HTTP exporter at `http://<prometheus>:9090/api/v1/otlp/v1/metrics`. `promote_resource_attributes` lifts the resource attributes you actually query on into labels; leaving `translation_strategy` at the default sanitizes names to the classic `[a-zA-Z_:][a-zA-Z0-9_:]*` form, while `NoTranslation` keeps the original dotted names and leans on the next feature.

## UTF-8 names and Remote Write 2.0

UTF-8 metric and label names are now allowed **by default** — `http.server.request.duration` is a legal series name, not just `http_server_request_duration`. Query the awkward ones with the new quoted syntax: `{"http.server.request.duration"}`.

Remote Write 2.0 is the other big protocol jump: it carries metadata, exemplars, created timestamps, and native histograms in-band, and interns repeated strings to shrink payloads. Senders and receivers negotiate the version, so a 3.0 sender still talks to a 2.x receiver — but you only get the new fields when both ends are 3.0.

## The breaking change hiding in PromQL

Range and lookback selectors are now **left-open and right-closed** (previously left-closed and right-closed). In practice a `rate(x[5m])` evaluation can include one fewer sample at the boundary, so subqueries and tightly-aligned ranges may return slightly different values than on 2.x. Nothing errors — the numbers just shift. Two more to grep for before upgrading:

- Regex `.` now matches newlines. A matcher like `msg=~".*"` behaves differently; use `[^\n]` for the old meaning.
- `holt_winters` was renamed `double_exponential_smoothing` and moved behind `--enable-feature=promql-experimental-functions`.

## Feature flags that now refuse to start... or do they

Several flags graduated to always-on, and passing them to `--enable-feature` now emits a warning rather than failing — but you should still delete them from your unit files: `agent`, `remote-write-receiver`, `promql-at-modifier`, `promql-negative-offset`, `new-service-discovery-manager`, `expand-external-labels`, `no-default-scrape-port`, `auto-gomemlimit`, `auto-gomaxprocs`. Agent mode and the remote-write receiver are now first-class, not experimental. Other operational notes: HTTP/2 for remote write is now **off** by default, Alertmanager must be 0.16.0+, logs are emitted via `slog` (structured) instead of go-kit, and a TSDB downgrade only works back to 2.55+.

Native histograms — the big storage-efficiency win — remain **experimental** and off by default; enable per-instance with `--enable-feature=native-histograms` and expect the format to keep evolving. The rewritten UI (cleaner, with a PromLens-style query tree) is default; if a dashboard breaks, `--enable-feature=old-ui` buys you time.

**Try next:** on a staging Prometheus, enable `--web.enable-otlp-receiver`, push one OTLP metric with a dotted name, and confirm you can graph it with `{"my.metric.name"}` — then diff a `rate()` panel against your 2.x instance to see the range-selector change in the wild.
