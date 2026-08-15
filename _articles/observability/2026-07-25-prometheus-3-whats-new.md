---
title: "Prometheus 3.0: what changed, and what it breaks"
date: 2026-07-25
track: observability
summary: "The first major Prometheus release in seven years ships a native OTLP endpoint, UTF-8 names, and a rewritten UI — plus a quiet PromQL change that shifts query results without erroring."
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

**Gist.** Prometheus 3.0, released **14 November 2024**, is the first major version since 2.0 in 2017, and most of its surface is additive: an OpenTelemetry Protocol (OTLP) receiver, UTF-8 metric and label names, Remote Write 2.0, a rewritten web interface. The one change that is not additive is a redefinition of the PromQL range selector interval, which is now **left-open and right-closed** where 2.x was left-closed and right-closed. That change costs nothing at startup and produces no error; it alters the sample set feeding `rate()` and similar functions at range boundaries, so the cost is paid in silently different numbers on dashboards and alert thresholds.

## Native OTLP ingestion

Prometheus 3.0 accepts OTLP metrics directly, removing the requirement for an OpenTelemetry Collector translating them first — previously the usual path was a Collector with the `prometheusremotewrite` exporter, or a Collector exposing a scrape endpoint for Prometheus to poll. The receiver is **off by default** and is enabled by a command-line flag rather than by configuration alone:

```bash
prometheus --web.enable-otlp-receiver \
  --config.file=/etc/prometheus/prometheus.yml
```

```yaml
# prometheus.yml
otlp:
  # keep dots in names instead of escaping them to underscores
  translation_strategy: NoUTF8EscapingWithSuffixes
  promote_resource_attributes:
    - service.name
    - service.namespace
    - service.instance.id
    - k8s.namespace.name
    - k8s.pod.name
```

Exporters speaking OTLP over HTTP target `http://<prometheus>:9090/api/v1/otlp/v1/metrics`.

Two settings carry the weight. `promote_resource_attributes` lifts named OpenTelemetry *resource* attributes — properties of the emitting entity, not of the individual data point — into Prometheus labels, which is what makes them available to selectors and aggregations. Attributes not promoted are not queryable as labels. `translation_strategy` decides how the OTLP name is mapped: the **default, `UnderscoreEscapingWithSuffixes`, escapes names into the classic `[a-zA-Z_:][a-zA-Z0-9_:]*` form**, replacing characters such as dots with underscores and appending unit and type suffixes, while `NoUTF8EscapingWithSuffixes` keeps the suffixes but preserves the original dotted name, and therefore depends on the UTF-8 name support described below.

The interaction between the two is the trap. Choosing `NoUTF8EscapingWithSuffixes` produces series whose names cannot be written as bare PromQL identifiers, so every existing query, recording rule and dashboard expression referring to the underscored form stops matching.

## UTF-8 names and the quoted selector

UTF-8 metric and label names are permitted **by default** in 3.0. `http.server.request.duration` is a legal series name, not only its sanitized cousin `http_server_request_duration`. Because the PromQL grammar cannot parse a dotted name as a bare identifier, such names are selected with quoted syntax placed inside the matcher braces:

```promql
{"http.server.request.duration"}
```

The quoted form is the metric name expressed as a matcher on the reserved `__name__` label rather than as an identifier token, which is why it composes with ordinary label matchers in the same braces.

## Remote Write 2.0

Remote Write 2.0 is the second protocol change. It carries **metadata, exemplars, created timestamps, and native histograms in-band**, and it interns repeated strings so that label names and values repeated across series are transmitted once and referenced. Sender and receiver **negotiate the protocol version over HTTP headers**, so a 3.0 sender remains able to write to a receiver that speaks only Remote Write 1.0; the new fields appear only when both ends support 2.0. A mixed fleet therefore degrades to the older payload rather than failing, which makes the absence of exemplars downstream a configuration symptom rather than a connectivity one.

One operational default changed alongside it: **HTTP/2 for remote write is off by default** in 3.0.

## The PromQL changes that alter results

The range selector interval is now **left-open and right-closed**. A window that in 2.x included the sample exactly at its left boundary now excludes it. For a `rate(x[5m])` evaluated on a scrape interval that divides evenly into the range, this can mean **one fewer sample in the window**, and subqueries or ranges tightly aligned to the scrape schedule are the most exposed. No error is raised; the value changes.

Two further changes are grep-able before an upgrade:

- **Regular-expression `.` now matches newlines.** A matcher such as `msg=~".*"` therefore matches multi-line values it previously skipped. `[^\n]` restores the earlier meaning.
- **`holt_winters` was renamed `double_exponential_smoothing`** and moved behind `--enable-feature=promql-experimental-functions`. A rule file still using the old name is an unknown function, not a silently different result.

## Feature flags and version floors

Several long-standing experimental flags graduated to always-on behaviour: `agent`, `remote-write-receiver`, `promql-at-modifier`, `promql-negative-offset`, `new-service-discovery-manager`, `expand-external-labels`, `no-default-scrape-port`, `auto-gomemlimit`, `auto-gomaxprocs`. Passing them to `--enable-feature` **emits a warning rather than failing startup**, so a stale unit file keeps working while accumulating log noise; removing them is cleanup, not a migration blocker. Agent mode and the remote-write receiver are first-class features in 3.0, no longer experimental.

Other constraints an upgrade plan must respect:

- **Alertmanager must be 0.16.0 or newer.**
- Logging is emitted through Go's structured `slog` package instead of go-kit, so log-parsing rules keyed to the old line format need revisiting.
- **A TSDB downgrade only works back to 2.55 or newer.** An instance upgraded from an older 2.x and then rolled back further than 2.55 cannot read its own on-disk time-series database, which makes 2.55 the mandatory staging point for a reversible upgrade.

**Native histograms** — the high-resolution histogram format — remain **experimental and off by default**, enabled per instance with `--enable-feature=native-histograms`, and the format continues to evolve. The rewritten user interface, which includes a PromLens-style query tree, is the default; `--enable-feature=old-ui` restores the previous one.

## Pitfalls

- Setting `translation_strategy: NoUTF8EscapingWithSuffixes` while dashboards still reference underscored names produces empty panels with no error: the series exist under their dotted names, and the underscored selectors match nothing.
- An OTLP resource attribute absent from `promote_resource_attributes` is not a label, so aggregations grouping by it collapse every series into one group rather than failing.
- A dotted metric name written as a bare PromQL identifier is a parse error; it must appear inside braces in quoted form.
- Exemplars and created timestamps silently disappear when either end of a remote-write link speaks only protocol version 1.0, because version negotiation downgrades the payload instead of rejecting the connection.
- `rate()` panels compared across a 2.x and a 3.0 instance differ at range boundaries because of the left-open interval, so alert thresholds tuned against 2.x values may fire or stop firing after the upgrade.
- A matcher relying on `.` to stop at a newline now spans the whole value, widening the match set without any syntax change.
- Rolling back to a 2.x release older than 2.55 leaves the TSDB unreadable by the downgraded binary.
