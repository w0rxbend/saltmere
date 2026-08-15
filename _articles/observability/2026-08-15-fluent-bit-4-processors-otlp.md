---
title: "Fluent Bit 4: Processors, the Content-Modifier Pipeline, and Routing Logs to OTLP"
date: 2026-08-15
track: observability
summary: "Fluent Bit's filters run globally and match by tag; the newer processors attach directly to one input or output and run in that plugin's own thread. In Fluent Bit 4 processors are the documented way to reshape logs before shipping them. This article covers the processor model — content_modifier, metrics_selector, sql, opentelemetry_envelope — and a YAML configuration that enriches logs and ships them to an OpenTelemetry collector over OTLP."
reading_time: 6
tags: [fluent-bit, processors, opentelemetry, otlp, log-pipeline, observability]
sources:
  - title: "Processors — Fluent Bit Official Manual"
    url: "https://docs.fluentbit.io/manual/data-pipeline/processors"
  - title: "Content modifier processor — Fluent Bit Official Manual"
    url: "https://docs.fluentbit.io/manual/data-pipeline/processors/content-modifier"
  - title: "OpenTelemetry envelope processor — Fluent Bit Official Manual"
    url: "https://docs.fluentbit.io/manual/data-pipeline/processors/opentelemetry-envelope"
  - title: "OpenTelemetry output plugin — Fluent Bit Official Manual"
    url: "https://docs.fluentbit.io/manual/data-pipeline/outputs/opentelemetry"
  - title: "Fluent Bit v4.2.0 release announcement"
    url: "https://fluentbit.io/announcements/v4.2.0/"
---

**Gist.** Fluent Bit's classic transformation stage — the filter — is a global component that selects records by tag pattern and executes in the engine's main thread, which couples transformation to routing and concentrates transformation cost in one thread. Fluent Bit 4 offers the **processor**: a transformation declared inside a single input or output block, scoped implicitly to that plugin's records and executed in that plugin's own thread. The cost of the change is that processors are **configurable only in YAML**, and that per-plugin threading makes an expensive transform compete with its own plugin's input/output (I/O) work rather than with the engine's main thread, so cost must be sized per plugin instead of globally.

## Processors compared with filters

A filter is a top-level component. It declares a `Match` pattern, and every record whose tag matches — regardless of which input produced it — traverses the filter. Selection is therefore late and string-based: the binding between a transformation and the data it transforms exists only as a glob over tags, evaluated at routing time.

A **processor** is declared *inside* an input or output definition. The attachment *is* the scope: the processor sees exactly the records of the plugin that contains it, and no `Match` expression is required or possible. Two consequences follow. First, a tag rename cannot silently detach a transformation from its data, because no tag expression mediates the binding. Second, the transformation executes in the containing plugin's thread, so its cost is charged to that plugin.

| | Processors | Filters |
|---|---|---|
| Where declared | Inside one input/output | Global, standalone |
| Record selection | Implicit (the plugin it is attached to) | `Match` tag pattern |
| Threading | Runs in the plugin's thread | Runs in the main thread |
| Configuration format | YAML only | YAML or classic |

The format restriction is load-bearing: **processors cannot be expressed in classic `.conf` syntax at all**, so a deployment on classic configuration files has to migrate to YAML before it can use any of them. The migration does not require rewriting every stage at once: **existing filters can be used as processors**, so a filter chain can be carried across and then replaced incrementally.

## The available processors

Fluent Bit 4 ships a small set, each addressing a distinct stage of reshaping.

- **content_modifier** — the general-purpose record editor. Its actions are `insert`, `upsert`, `delete`, `rename`, `hash` (SHA-256), `extract` (a regular expression producing key/value pairs), and `convert` (change a field's type). It operates on log and trace **bodies and attributes**, which is what makes it usable both for enrichment (adding resource-level identity) and for redaction (removing or hashing a field before it leaves the process).
- **metrics_selector** — retains or discards metrics by name or by regular expression. Because it can be attached to the input, **cardinality is trimmed at the source**, before series are ever materialised downstream.
- **sql** — evaluates a `SELECT ... WHERE` expression over the record's fields, expressing projection (which fields survive) and filtering (which records survive) in a single statement.
- **opentelemetry_envelope** — wraps records in the OpenTelemetry log schema. It is required whenever the data did **not** originate from the OpenTelemetry input, because only then is the record already in that schema.

Processors are grouped by signal under `logs:`, `metrics:`, or `traces:`, so one plugin can carry a different transformation chain per telemetry type without any tag arithmetic.

## Enrichment and OTLP export

The following YAML pipeline tails an application log; uses three **content_modifier** steps to insert a static `service.name`, hash a field carrying personally identifiable information (PII), and delete an internal token; wraps the result with **opentelemetry_envelope**; and exports it with the **opentelemetry** output plugin over the OpenTelemetry Protocol (OTLP).

```yaml
service:
  flush: 1
  log_level: info

pipeline:
  inputs:
    - name: tail
      path: /var/log/app/*.log
      tag: app.logs

      processors:
        logs:
          - name: content_modifier
            action: insert
            key: service.name
            value: checkout

          - name: content_modifier
            action: hash
            key: user_email

          - name: content_modifier
            action: delete
            key: internal_token

          # Wrap records in the OTel log schema for the OTLP output
          - name: opentelemetry_envelope

  outputs:
    - name: opentelemetry
      match: "*"
      host: otel-collector
      port: 4318
      logs_uri: /v1/logs
      tls: on
      header:
        - Authorization Bearer ${OTLP_TOKEN}
```

Three properties of this configuration are worth stating explicitly.

**Ordering.** The three `content_modifier` steps execute **in the declared order, on the tail input's thread, before routing**. The redaction invariant this establishes is narrow but real: `user_email` is hashed and `internal_token` is removed while the record is still inside the input plugin, so no output — including one added later to the same pipeline — can observe the original values. A filter placed after routing would not offer that guarantee, because ordering relative to other tag-matched stages is a property of the whole pipeline rather than of one plugin.

**Envelope.** `opentelemetry_envelope` is what makes the OTLP export valid. Records from a non-OpenTelemetry input, such as `tail`, are not in the schema the collector expects, and **without the envelope step the collector receives malformed or empty logs**. The transport-level export can still succeed, so the deficiency is visible at the receiver rather than at the sender.

**Endpoint.** The output targets port **4318**, the OTLP over HTTP port, with the default `logs_uri` of **`/v1/logs`** taken from the OTLP specification. Any OTLP-compatible endpoint may be named in `host`: an OpenTelemetry Collector, or a backend ingesting OTLP directly.

Configuration can be checked without starting the pipeline:

```bash
fluent-bit --dry-run --config fluent-bit.yaml
```

The threading trade-off follows from the scoping decision. Because a processor runs in its plugin's thread, an expensive `sql` or `content_modifier` step on a hot input competes with that input's own I/O rather than with a shared pool. The effect is isolation — a slow transform on one noisy source cannot stall unrelated pipelines — but the corollary is that transformation cost must be budgeted per plugin. OTLP export over HTTP carries serialisation overhead in addition to transport; at high log rates, batching and proximity to the collector both matter.

## Pitfalls

- **A classic `.conf` deployment silently has no processors.** Processors are YAML-only; a `processors:` block has no representation in classic syntax, so the transformation never runs and no equivalent stage exists to fall back on.
- **Omitting `opentelemetry_envelope` on a non-OpenTelemetry input produces empty or malformed logs at the collector, not an error at the sender.** The records are not in the OpenTelemetry log schema, and the OTLP output emits them regardless.
- **Placing redaction in a global filter rather than an input processor widens the exposure window.** A filter runs after routing and its ordering is a pipeline-wide property, so a record can reach an output with the unredacted field still present.
- **Reordering `content_modifier` steps changes the result.** A `delete` of a key placed before the `hash` that reads it leaves nothing to hash; the steps execute in declaration order, not by dependency.
- **`metrics_selector` attached to an output trims cardinality after the series have already been carried through the pipeline.** Attaching it to the input is what removes the work rather than the export.
- **A tag rename breaks a filter's `Match` but cannot break a processor's binding.** Diagnosing a transformation that stopped running after a tag change points at filters, since processors have no tag expression to invalidate.
