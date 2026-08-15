---
title: "OTTL: reshaping and redacting telemetry in the OpenTelemetry Collector"
date: 2026-07-30
track: observability
summary: "The transform processor rewrites spans, metrics, and logs in flight using OTTL — a statement language built from contexts, path expressions, and conditions. It is the pipeline stage where personally identifiable information is stripped, attributes normalized, and fields derived before data leaves the Collector."
reading_time: 6
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

**Gist.** Instrumentation emits telemetry in whatever shape the instrumented library chose: an authorization header lands in a span attribute, a customer identifier rides inside a uniform resource locator (URL), a metric carries the wrong unit, a log's severity sits in the body rather than the severity field. Correcting that at the source requires redeploying every service; the **transform processor** corrects it centrally by running statements written in **OTTL, the OpenTelemetry Transformation Language**, against every record passing through the Collector. The cost is that the rewrite is an ordered, in-process mutation stage: statements execute top to bottom on the hot path, a statement can destroy the input another statement needs, and behaviour on a failed statement depends entirely on the configured error mode.

## The three constructs

**Contexts.** Telemetry is nested — a resource holds scopes, a scope holds spans, a span holds attributes and events, a metric holds data points. An OTTL statement executes in a **context** naming the level being edited: `resource`, `scope`, `span`, `spanevent`, `metric`, `datapoint`, `log`. Statements are grouped per signal (`trace_statements`, `metric_statements`, `log_statements`) and the level is selected with a `context:` key on each block. Paths in the current style are written with the context as a prefix (`span.attributes["k"]`); the older style omitted the prefix and wrote `attributes["k"]` against the block's context. **The context determines the iteration unit**: a `span` block runs its statements once per span, a `datapoint` block once per data point, so the same edit expressed at a coarser context touches fewer items and at a finer context multiplies the work.

**Paths.** Within a context, fields are addressed by dotted paths: `span.name`, `span.status.code`, `span.attributes["http.route"]`, `resource.attributes["service.name"]`, `log.body`, `metric.unit`. **A path is both getter and setter** — the expression that reads a field is the expression that assigns to it.

**Statements and conditions.** A statement is a **function call**, optionally guarded by a `where` clause. `set`, `delete_key`, `keep_keys`, `replace_pattern`, `truncate_all` and `limit` are editors that mutate the record; `where` restricts which records an editor touches, using boolean expressions and converters such as `IsMatch`. **Statements inside a block run in declaration order**, which makes ordering a correctness property rather than a stylistic one.

## A configuration that redacts and normalizes

The following `transform` processor performs four independent jobs: removal of an authorization attribute, redaction of an email address embedded in a URL, a bound on attribute count, and correction of a metric unit. It runs under `otelcol-contrib`, the distribution that ships the contrib processors.

```yaml
processors:
  transform:
    error_mode: ignore        # a failing statement is logged; the next statement still runs
    trace_statements:
      - context: span
        statements:
          # 1. Remove a sensitive header attribute outright.
          - delete_key(span.attributes, "http.request.header.authorization")

          # 2. Redact an email embedded in the URL, preserving the surrounding shape.
          - replace_pattern(span.attributes["url.full"],
              "[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+", "[EMAIL]")

          # 3. Derive a coarse status class before the raw code can be dropped.
          - set(span.attributes["http.status_class"], "5xx")
              where span.attributes["http.response.status_code"] >= 500

          # 4. Bound attribute cardinality reaching the backend index.
          - limit(span.attributes, 40, [])
    metric_statements:
      - context: metric
        statements:
          # Correct a unit reported as bytes when the values are mebibytes.
          - set(metric.unit, "MiBy") where metric.name == "process.memory.usage_mib"
    log_statements:
      - context: log
        statements:
          # Promote a severity carried in the body into the severity field.
          - set(log.severity_text, "ERROR")
              where IsMatch(log.body, ".*(exception|panic|fatal).*")

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [transform, batch]
      exporters: [otlphttp]
```

Three properties of this configuration are load-bearing.

**Error mode selects the failure domain.** The processor documents three modes. With `error_mode: ignore`, an error raised by a statement is logged and evaluation continues with the next statement; `silent` behaves the same way but does not log; with `propagate`, the error is returned up the pipeline and the payload is dropped by the Collector. The practical split is `ignore` where continuity of the telemetry stream matters and `propagate` while a configuration is being tested, because a fault is then impossible to miss.

**The `where` clause is what keeps an editor surgical.** Statement 3 tags only spans whose response status code is at least 500; the log statement rewrites severity only on bodies matching the pattern. Without a guard, an editor applies to every record the context iterates.

**Order is semantics, not layout.** A pipeline that both derives a route from a URL and redacts that URL must derive first: `replace_pattern` mutates the attribute in place, so a derivation placed after it reads the already-redacted value. The same asymmetry applies to `limit` and `keep_keys`, which remove attributes that later statements would otherwise read.

## Position relative to neighbouring components

OTTL is not confined to the transform processor. The **filter processor** evaluates OTTL conditions to *drop* telemetry, and routing and tail-sampling components accept OTTL expressions as well (see the tail-based-sampling article in this track), so the path and condition syntax is learned once and reused across the pipeline. The division of labour is direct: the transform processor **modifies** records — redact, enrich, normalize, reshape — while the filter processor **removes** them. For personally identifiable information specifically, the placement argument is that a field scrubbed in the Collector before the exporter never reaches a backend that would index and retain it.

**Verification path.** Run `otelcol-contrib` with the configuration above and a `debug` exporter, emit a test span carrying a fabricated `authorization` header and an email address inside `url.full` — `telemetrygen traces` generates such traffic — and confirm the exported span lacks the header and shows `[EMAIL]` in place of the address. Introducing a deliberately wrong path and switching `error_mode` between `ignore` and `propagate` demonstrates the difference in failure behaviour.

## Pitfalls

- A misspelled attribute key is not a syntax error but a key that no record carries, so the statement matches nothing and the intended edit never happens; the telemetry arrives looking untouched rather than raising a failure.
- `replace_pattern` mutates the attribute in place, so any statement that derives a value from the same attribute must precede it; placed after, it reads the redacted string and derives the wrong value.
- `limit` and `keep_keys` remove attributes, so a statement ordered after them may find its input already gone even though the path is spelled correctly.
- The chosen context sets the iteration unit: statements placed in a `datapoint` block execute once per data point rather than once per metric, multiplying the per-record cost of an expensive converter such as a regular-expression match.
- A regular expression written for redaction matches only what it was written to match; an address format outside the pattern passes through unredacted, and the resulting record looks correctly processed because no statement failed.
- Statements addressing `resource.attributes` from a `span` context and from a `resource` context differ in how often they run, so an edit expressed at the span level is repeated for every span sharing that resource.
- `error_mode: propagate` turns one failing statement into a pipeline error that drops the payload, which is the intended behaviour while a configuration is being tested and a source of data loss when left enabled in production.
- `error_mode: silent` suppresses the log line that `ignore` still emits, so a statement failing on every record leaves no trace at all in the Collector's own output.
