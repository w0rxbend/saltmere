---
title: "Vector: observability pipelines with sources, transforms, and VRL"
date: 2026-08-11
track: observability
summary: "Vector is a vendor-agnostic Rust pipeline for logs and metrics: sources feed transforms feed sinks. The remap transform, powered by the Vector Remap Language (VRL), parses, reshapes, redacts, and drops events before they reach a backend. The topology model and an end-to-end configuration."
reading_time: 6
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

**Gist.** Observability backends charge for ingest and retention on data that is never queried — debug lines, health-check chatter, repeated stack traces. Vector interposes a processing stage between emitters and backends: a directed graph of sources, transforms, and sinks in which the `remap` transform, written in the Vector Remap Language (VRL), parses, redacts, and discards events before the priced stage. The cost is that pipeline logic moves out of the backend's query language into a separate deployed process whose own failures — a dropped event, a silently erroring transform — are invisible from the backend that never received the data.

**Vector** is an observability data pipeline written in Rust, open-sourced and maintained by **Datadog**, and vendor-agnostic in its component set. It occupies the same position as Logstash and Fluentd. It ships as a single binary deployable as a host agent, a sidecar, or an aggregator tier.

## The topology: sources → transforms → sinks

Vector models a pipeline as a directed graph of three component kinds. **Sources** ingest — `file`, `kafka`, `journald`, `docker_logs`, `http_server`, `demo_logs` for testing, and an `opentelemetry` source speaking the OpenTelemetry Protocol (OTLP). **Transforms** reshape: parse, filter, sample, aggregate, route. **Sinks** emit — `loki`, `elasticsearch`, `aws_s3`, `prometheus_remote_write`, `kafka`, `console`, and an `opentelemetry` sink that normalizes back to OTLP.

Components are wired by name through each one's `inputs` field. **Edges are name references rather than a distinct construct**, so fan-out (one source, many sinks) and fan-in (many transforms, one sink) require no special syntax: a sink lists several inputs, or several sinks list the same input. This is what makes an **aggregator** topology cheap to express — collect from everywhere, transform once, then route the identical post-transform stream to a low-cost archive in object storage and a hot store in Loki simultaneously, without duplicating the transform.

Logs and metrics traverse the same graph as events, which lets Vector sit adjacent to an OpenTelemetry deployment: receive OTLP, trim, re-emit OTLP.

## remap and VRL

`remap` is the general-purpose transform, and it executes **VRL**, the Vector Remap Language. VRL is an expression-oriented domain-specific language whose programs are **compiled and type-checked when the configuration loads**, rather than parsed afresh per event. Three constraints define its shape:

- **Stateless.** A program observes one event at a time. Cross-event work — deduplication, aggregation — belongs to `reduce`, `aggregate`, or `dedupe`, not to `remap`.
- **Type-safe at compile time.** Field types are inferred and checked before the pipeline starts.
- **Fail-safe.** **Every fallible function call must have its error handled, or the configuration does not boot.** The failure surfaces at startup, not at the first malformed line in production.

The fail-safety rule drives the syntax. `parse_json` can fail on malformed input, so a call is either handled explicitly — `parsed, err = parse_json(.message)` binds the result and the error separately — or asserted infallible with a trailing `!`, as in `parse_json!(.message)`, which **aborts the event when parsing fails**. The event root is `.`; fields are paths such as `.user.id`; `del()` removes a field. The following program replaces the event with parsed JavaScript Object Notation (JSON), enriches it, redacts two sensitive fields, and discards debug records:

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

The `??` operator supplies a fallback when its left operand errors. `abort` halts processing of the event; with the transform's default `drop_on_abort = true` the event is discarded rather than forwarded. **The volume reduction of the whole pipeline concentrates in that one statement**: the `del()` calls shrink each event, the `abort` removes events entirely.

## An end-to-end configuration

The following TOML wires a `file` source into a `remap` transform, then a `sample` transform that thins routine informational logs, and finally two sinks — Loki for retained data and `console` for local inspection.

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

Two details carry the design. First, `exclude` on `sample` is a VRL condition marking events that **always** pass: errors and warnings bypass the 1-in-10 rate while informational logs are reduced by 90%. Second, the two sinks take different inputs — `loki` reads the sampled stream, `console` reads the unsampled output of `parse` — which is the fan-out described above, expressed as nothing more than two different names in `inputs`.

`vector validate config.toml` type-checks the VRL programs ahead of deployment, so a fail-safety violation or a type error is reported at validation time rather than at process start on a production host.

| Job | Component | Volume effect |
|---|---|---|
| Parse & redact | `remap` (VRL) | shrinks each event |
| Drop noise | `remap` `abort` / `filter` | removes events |
| Statistical thinning | `sample` | keeps 1-in-N |
| Collapse duplicates | `reduce` / `aggregate` | merges events |
| Split by destination | `route` | fan-out to many sinks |

## Observing the pipeline itself

A pipeline that discards the wrong events fails silently, because the evidence of the failure is precisely the data that never arrived. Vector ships two introspection commands against a running instance. `vector top` renders a `top`-style terminal interface of per-component throughput, error counts, and bytes in and out, which exposes a transform that is erroring on every event while still appearing configured. `vector tap` samples events flowing *between* named components — `vector tap parse` prints what leaves the remap stage — so a redaction can be confirmed without redeployment.

## Where it sits among neighbours

Relative to the [Grafana Alloy](/articles/observability/2026-07-26-grafana-alloy-collector) and [OTTL transform processor](/articles/observability/2026-07-30-ottl-transform-processor) articles, Vector's remap stage is the same category of component as OTTL — an in-pipeline rewrite language — with a different data model. **OTTL is a statement language scoped to OpenTelemetry contexts** (span, metric, datapoint) and bound to the Collector's data model, so a statement addresses a signal-specific field. **VRL is a general expression language over a free-form event**, which accommodates unstructured logs but carries no native notion of a span or a datapoint. OTLP-native tooling fits a deployment committed to OpenTelemetry semantics; Vector fits a backend-agnostic aggregator that ingests heterogeneous inputs and fans out to several stores.

A useful experiment: replace the `file` source with `type = "demo_logs"` and `format = "json"`, run `vector --config config.toml` alongside `vector top`, then remove the `if .severity == "debug" { abort }` line. The change in throughput and in the event count reaching the Loki sink measures what the drop removes.

## Pitfalls

- **A fallible call asserted with `!` discards the event on failure.** `parse_json!(.message)` on a line that is not JSON aborts the event, so a source emitting a mix of JSON and plain text loses the plain-text half silently. The handled form, `parse_json(.message)`, keeps the error available for a fallback path.
- **`abort` only drops the event when `drop_on_abort` is true.** With it set false, the aborted event continues through the pipeline unmodified, so a redaction that ran before the `abort` may not have applied and the unredacted event reaches the sink.
- **`remap` is stateless, so cross-event logic written there does not work.** Deduplication and aggregation require `dedupe`, `reduce`, or `aggregate`; a `remap` program has no view of any event but the current one.
- **`sample` without `exclude` thins errors at the same rate as informational logs.** The rate applies uniformly, so a 1-in-10 sample drops nine of every ten error records unless a condition exempts them.
- **Type errors and unhandled fallibility abort startup, not the event.** A configuration edited in place and not passed through `vector validate` can prevent the process from booting after a restart that happens hours later, when the cause is no longer the most recent change in anyone's memory.
- **A silently erroring transform looks identical to a quiet source at the backend.** Only `vector top` or `vector tap` distinguishes "no events were produced" from "every event failed the transform", because both present as absent data downstream.
