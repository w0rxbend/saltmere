---
title: "OTTL: reshaping and redacting telemetry in the OpenTelemetry Collector"
date: 2026-07-30
track: observability
summary: "The transform processor lets you rewrite spans, metrics, and logs in flight using OTTL — a small statement language with contexts, path expressions, and conditions. It's how you strip PII, normalize attributes, and derive fields before data ever leaves the Collector. Here's the mental model and a config you can drop in."
reading_time: 5
tags: [opentelemetry, otel-collector, ottl, transform-processor, pii, redaction]
sources:
  - title: "Transforming telemetry — OpenTelemetry Collector documentation"
    url: "https://opentelemetry.io/docs/collector/transforming-telemetry/"
  - title: "Transform Processor — opentelemetry-collector-contrib (README)"
    url: "https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/transformprocessor/README.md"
  - title: "OTTL functions — opentelemetry-collector-contrib (ottlfuncs)"
    url: "https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/pkg/ottl/ottlfuncs/README.md"
  - title: "Handle Sensitive Information with the OpenTelemetry Collector — Honeycomb Docs"
    url: "https://docs.honeycomb.io/send-data/opentelemetry/collector/handle-sensitive-information"
---

Instrumentation gives you *some* telemetry, but rarely in the exact shape you want: an auth token leaks into a span attribute, a URL carries a customer id you can't store, a metric's units are wrong, a log's severity lives in the body instead of the field. Fixing all of that at the source means redeploying every service. Fixing it at the **Collector** — one config, one restart — is why the **transform processor** and its language, **OTTL** (the OpenTelemetry Transformation Language), exist. It's the general-purpose rewrite stage in your pipeline, sitting between receivers and exporters.

## OTTL in three ideas

**Contexts.** Telemetry is nested — a resource holds spans, a span holds attributes and events; a metric holds data points. OTTL statements run in a **context** that picks the level you're editing: `resource`, `scope`, `span`, `spanevent`, `metric`, `datapoint`, `log`. You group statements under the signal (`trace_statements`, `metric_statements`, `log_statements`) and address a context with `context:` (newer config style) or the older per-context list. The context decides what `attributes[...]` and the path expressions refer to.

**Paths.** Inside a context you read and write fields with dotted paths: `span.name`, `span.status.code`, `span.attributes["http.route"]`, `resource.attributes["service.name"]`, `log.body`, `metric.unit`. These are both getters and setters — the same path you read is the one you assign.

**Statements and conditions.** A statement is a **function call**, optionally guarded by a `where` clause. `set(...)`, `delete_key(...)`, `keep_keys(...)`, `replace_pattern(...)`, `truncate_all(...)`, `limit(...)` are editors; `where` filters which items they touch using boolean expressions and converters like `IsMatch(...)`. Statements in a block run **top to bottom**, so order matters — derive a value first, then redact the field you derived it from.

## A config that redacts and normalizes

Here's a `transform` processor doing four common jobs at once: strip an auth token, scrub an email out of a URL, cap attribute count to control cardinality, and fix a metric's units. Drop it into a Collector (`otelcol-contrib`) pipeline.

```yaml
processors:
  transform:
    error_mode: ignore        # a bad statement skips that item, doesn't crash the batch
    trace_statements:
      - context: span
        statements:
          # 1. Remove a sensitive header attribute outright.
          - delete_key(span.attributes, "http.request.header.authorization")

          # 2. Redact an email embedded in the URL, keep the shape.
          - replace_pattern(span.attributes["url.full"],
              "[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+", "[EMAIL]")

          # 3. Derive a coarse status class, THEN you could drop the raw code.
          - set(span.attributes["http.status_class"], "5xx")
              where span.attributes["http.response.status_code"] >= 500

          # 4. Bound attribute cardinality to protect your backend's index.
          - limit(span.attributes, 40, [])
    metric_statements:
      - context: metric
        statements:
          # Fix a mislabeled unit reported as bytes but actually mebibytes.
          - set(metric.unit, "MiBy") where metric.name == "process.memory.usage_mib"
    log_statements:
      - context: log
        statements:
          # Promote a severity carried in the body up to the real field.
          - set(log.severity_text, "ERROR")
              where IsMatch(log.body, ".*(exception|panic|fatal).*")

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [transform, batch]
      exporters: [otlphttp]
```

A few things worth internalizing from this. `error_mode: ignore` is the safe default in production — a malformed record (missing the attribute a statement expects) is skipped rather than failing the whole batch; use `propagate` in testing when you *want* loud failures. The `where` clause is what keeps a statement surgical: statement 3 only tags server errors, statement 5 only re-severities logs that look like errors. And **statement order is load-bearing** — if you wanted to redact the raw URL *and* derive a route from it, you'd derive first, redact second, because the second statement destroys the input to the first.

## Where it sits vs. its neighbors

OTTL shows up in more than the transform processor — the **filter processor** uses OTTL conditions to *drop* telemetry, and the **routing** and **tail-sampling** components (see the tail-based-sampling article here) use OTTL expressions too — so learning the path/condition syntax once pays off across the pipeline. Reach for the transform processor when you need to **modify** data (redact, enrich, normalize, reshape); reach for the filter processor when you need to **remove** it; and keep genuinely high-volume, hot-path redaction as close to the edge as you can. The rule of thumb for PII especially: scrub it in the Collector *before* the exporter, so sensitive fields never reach a backend where they'd be indexed, retained, and subpoena-able.

**Try next:** Run `otelcol-contrib` with the config above and a `debug`/`logging` exporter, fire a test span carrying a fake `authorization` header and an email in `url.full` (the `telemetrygen traces` tool is handy), and watch the exported span come out with the header gone and the email replaced by `[EMAIL]` — then add a deliberately wrong path and flip `error_mode` between `ignore` and `propagate` to feel the difference in failure behavior.
