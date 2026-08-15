---
title: "Fluent Bit 4: Processors, the Content-Modifier Pipeline, and Routing Logs to OTLP"
date: 2026-08-15
track: observability
summary: "Fluent Bit's filters run globally and match by tag; the newer processors attach directly to one input or output and run in that plugin's own thread. In Fluent Bit 4 (v4.2.0, released 24 September 2025) that's the idiomatic way to reshape logs before shipping them. Here's the processor model — content_modifier, metrics_selector, sql, opentelemetry_envelope — and a YAML config that enriches logs and ships them to an OTel collector over OTLP."
reading_time: 5
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

Fluent Bit's classic pipeline is inputs → **filters** → outputs, where a filter is a global stage that grabs records by **tag match** and runs in the engine's main thread. That works, but it couples transformation to routing and it makes the main thread the bottleneck under load. Fluent Bit 4 promotes a cleaner primitive: the **processor**, attached directly to a single input or output, running in *that plugin's* thread. It's the same job — reshape a record — with a better place to stand. The current line is **v4.2.0**, released **24 September 2025**.

## Processors vs. filters

A filter is defined as its own top-level component and selects records with a `Match` pattern against tags; every matching record from anywhere in the pipeline flows through it. A **processor** is declared *inside* an input or output block, so it only ever sees that plugin's data — no tag matching needed, because the attachment *is* the scope. It also executes in the plugin's own thread, so transformation cost scales with the plugin instead of piling onto one shared thread.

| | Processors | Filters |
|---|---|---|
| Where declared | Inside one input/output | Global, standalone |
| Record selection | Implicit (the plugin it's attached to) | `Match` tag pattern |
| Threading | Runs in the plugin's thread | Runs in the main thread |
| Config format | YAML only | YAML or classic |

One caveat up front: processors are **YAML-only**. If you're still on classic `.conf` files, this is the feature that finally justifies the migration. (Legacy filters can still be invoked *as* processors, so nothing is lost in the move.)

## The processors worth knowing

Fluent Bit 4 ships a focused set. The ones you'll reach for:

- **content_modifier** — insert, upsert, delete, rename, `hash` (SHA-256), `extract` (regex to key/values), and `convert` (change a field's type) on log/trace bodies and attributes. This is the workhorse.
- **metrics_selector** — keep or drop metrics by name or regex, trimming cardinality at the source.
- **sql** — run a `SELECT ... WHERE` over the record's fields to project and filter in one expression.
- **opentelemetry_envelope** — wrap records in the OpenTelemetry log schema so an OTLP output can emit them correctly. Needed whenever the data *didn't* come from the OpenTelemetry input.

Processors are grouped by signal under `logs:`, `metrics:`, or `traces:`, so a single input can carry different transformations for each telemetry type.

## Enrich and ship to OTLP

Here's a complete YAML pipeline. It tails an app log, uses **content_modifier** to add a static `service.name`, hash a PII field, and drop an internal token; wraps the result with **opentelemetry_envelope**; and ships it to an OpenTelemetry collector with the **opentelemetry** output plugin.

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

A few things to notice. The three `content_modifier` steps run **in order** on the tail input's thread, before routing — so redaction happens before the record can leak downstream. The `opentelemetry_envelope` processor is what makes the OTLP output valid: without it, records from a non-OTel input aren't in the schema the collector expects, and logs arrive malformed or empty. The output targets port **4318** (OTLP/HTTP) with the default `logs_uri` of **`/v1/logs`** — the path from the OTLP spec. Point `host` at any OTLP-compatible endpoint: an OpenTelemetry Collector, or a backend that ingests OTLP directly.

Validate the file before deploying:

```bash
fluent-bit --dry-run --config fluent-bit.yaml
```

The trade-off worth stating: because a processor runs in its plugin's thread, an expensive `sql` or `content_modifier` step on a very hot input competes with that input's own I/O rather than a shared pool. That's usually the *right* isolation — a slow transform on one noisy source can't stall every other pipeline — but it means you size the cost per input, not globally. And OTLP export is HTTP with real serialization overhead; for very high log rates, batch aggressively and keep the collector close.

**Try next:** swap the `tail` input for `name: dummy` with a canned JSON message, run the pipeline against a local OpenTelemetry Collector's `4318` receiver, and confirm in the collector's debug exporter that `service.name` is present and `user_email` arrives hashed — proof the processors ran before export.
