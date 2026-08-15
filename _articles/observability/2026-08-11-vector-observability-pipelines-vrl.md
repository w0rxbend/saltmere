---
title: "Vector: building observability pipelines with sources, transforms, and VRL"
date: 2026-08-11
track: observability
summary: "Vector is a fast, vendor-agnostic Rust pipeline for logs, metrics, and traces: sources feed transforms feed sinks. The remap transform, powered by VRL, is where you parse, reshape, redact, and drop events — cutting volume and cost before data ever hits a backend. Here's the topology model and an end-to-end config you can run."
reading_time: 5
tags: [vector, vrl, observability, log-pipeline, loki, cost-control]
sources:
  - title: "Vector documentation — vector.dev"
    url: "https://vector.dev/docs/"
  - title: "VRL (Vector Remap Language) reference"
    url: "https://vector.dev/docs/reference/vrl/"
  - title: "remap transform — Vector documentation"
    url: "https://vector.dev/docs/reference/configuration/transforms/remap/"
  - title: "vectordotdev/vector (GitHub)"
    url: "https://github.com/vectordotdev/vector"
  - title: "Vector | Datadog Open Source Hub"
    url: "https://opensource.datadoghq.com/projects/vector/"
---

Most log bills are paid on data nobody reads. Debug lines, health-check chatter, duplicated fields, and stack traces that repeat a thousand times an hour all get shipped, indexed, and retained at full price. The fix is a processing stage between your services and your backend — one that parses, trims, and drops before the expensive part. **Vector** is that stage: a high-performance observability data pipeline written in Rust, open-sourced and maintained by **Datadog**, and vendor-agnostic by design. It's a lighter, faster alternative to (or complement for) Logstash and Fluentd, and as of the **v0.57.0** release (July 2026) it's a stable, single-binary tool you can drop into a host, a sidecar, or an aggregator tier.

## The topology: sources → transforms → sinks

Vector models a pipeline as a directed graph of three component kinds. **Sources** ingest data — `file`, `kafka`, `journald`, `docker_logs`, `http_server`, `demo_logs` for testing, and an `opentelemetry` source that speaks OTLP. **Transforms** reshape it — parse, filter, sample, aggregate, route. **Sinks** ship it out — `loki`, `elasticsearch`, `aws_s3`, `prometheus_remote_write`, `kafka`, `console`, and an `opentelemetry` sink for normalizing to OTLP. You wire components by name through each one's `inputs` field, so fan-out (one source to many sinks) and fan-in are just references, not special cases. That makes Vector a natural **aggregator**: collect from everywhere, transform once, then reroute the same stream to a cheap archive in S3 and a hot store in Loki simultaneously.

Because every event carries logs *or* metrics through the same graph, Vector sits comfortably next to OpenTelemetry — it can act as an OTel-adjacent aggregator, receiving OTLP and re-emitting it after trimming.

## remap and VRL: the workhorse transform

The transform you'll reach for most is `remap`, and it runs **VRL** — the Vector Remap Language. VRL is a small, expression-oriented DSL that compiles to Rust with no runtime interpreter, so it's fast enough to run on every event. It's deliberately constrained: **stateless** (one event at a time), **type-safe** at compile time, and **fail-safe** — any fallible function must have its error handled, or your config won't boot.

That last point drives the syntax. A function like `parse_json` can fail on malformed input, so you either handle the error explicitly (`parse_json(.message)` returns a `{value, error}` pair) or assert it can't fail with a trailing `!` (`parse_json!(.message)`, which aborts the event on failure). The event root is `.`, fields are paths like `.user.id`, and `del()` removes them. Here's a VRL program that parses a raw JSON line into the event root, enriches it, redacts two sensitive fields, and drops debug noise entirely:

```coffee
. = parse_json!(.message)          # replace the event with parsed JSON
.service = "checkout"              # add a static field
.severity = downcase(to_string(.level) ?? "info")  # normalize, with a default
del(.authorization)                # redact a bearer token
del(.card_number)                  # redact PII outright
if .severity == "debug" {
  abort                            # drop this event from the pipeline
}
```

The `??` operator supplies a fallback when the left side errors or is null. `abort` stops processing and, with the transform's default `drop_on_abort = true`, discards the event — that single line is where a large slice of your volume reduction comes from.

## An end-to-end config you can run

This TOML wires a `file` source into a `remap` transform, then a `sample` transform to thin out high-cardinality info logs, and finally two sinks — Loki for the useful data and a `console` sink so you can watch it during development.

{% raw %}
```toml
[sources.app_logs]
type = "file"
include = ["/var/log/app/*.log"]

[transforms.parse]
type = "remap"
inputs = ["app_logs"]
drop_on_abort = true
source = '''
  . = parse_json!(.message)
  .service  = "checkout"
  .severity = downcase(to_string(.level) ?? "info")
  del(.authorization)
  del(.card_number)
  if .severity == "debug" { abort }
'''

[transforms.thin]
type = "sample"
inputs = ["parse"]
rate = 10                                     # keep 1 of every 10...
exclude = '.severity == "error" || .severity == "warn"'  # ...but never drop these

[sinks.loki]
type = "loki"
inputs = ["thin"]
endpoint = "http://loki:3100"
  [sinks.loki.labels]
  service  = "{{ service }}"
  severity = "{{ severity }}"
  [sinks.loki.encoding]
  codec = "json"

[sinks.debug]
type = "console"
inputs = ["parse"]
  [sinks.debug.encoding]
  codec = "json"
```
{% endraw %}

Note `exclude` on the `sample` transform: it's a VRL condition marking events that must **always** pass, so errors and warnings survive sampling while routine info logs are cut 90%. Validate before deploying with `vector validate config.toml`, which type-checks the VRL at startup rather than at 3 a.m.

| Job | Component | Volume effect |
|---|---|---|
| Parse & redact | `remap` (VRL) | shrinks each event |
| Drop noise | `remap` `abort` / `filter` | removes events |
| Statistical thinning | `sample` | keeps 1-in-N |
| Collapse duplicates | `reduce` / `aggregate` | merges events |
| Split by destination | `route` | fan-out to many sinks |

## Observing the pipeline itself

A pipeline that silently drops the wrong events is worse than no pipeline. Vector ships two live introspection tools. `vector top` gives a `top`-style TUI of per-component throughput, error counts, and bytes in/out — the fastest way to see a transform quietly erroring. `vector tap` lets you sample events flowing *between* named components on a running instance (`vector tap parse` prints what leaves the remap stage), so you can confirm a redaction actually fired without redeploying.

## Where it sits among neighbors

If you've read the [Grafana Alloy](/articles/observability/2026-07-26-grafana-alloy-collector) and [OTTL transform processor](/articles/observability/2026-07-30-ottl-transform-processor) pieces here, Vector's remap stage is the same *idea* as OTTL — an in-pipeline rewrite language — but a different shape. **OTTL** is a statement language scoped to OpenTelemetry contexts (span, metric, datapoint), tightly bound to the Collector's data model. **VRL** is a general expression language over a free-form event, more flexible for messy logs but not natively signal-aware. Reach for OTLP-native tooling (Alloy, the Collector, OTTL) when you're all-in on OpenTelemetry semantics; reach for Vector when you want a fast, backend-agnostic aggregator that ingests anything, cuts cost aggressively, and fans out to whichever stores you run.

**Try next:** Swap the `file` source for `type = "demo_logs"` with `format = "json"`, run `vector --config config.toml` alongside `vector top` in a second terminal, then delete the `if .severity == "debug" { abort }` line and watch the throughput and Loki-bound event count jump — that delta is the money the drop is saving you.
