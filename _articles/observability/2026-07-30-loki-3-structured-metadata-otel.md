---
title: "Loki 3: Structured Metadata and OTel-Native Logs"
date: 2026-07-30
track: observability
summary: "Loki 3.0 fixed the label-cardinality trap with structured metadata and added a native OTLP endpoint. Here's why trace_id must not be a label, how OTel attributes map onto Loki, and a working export config plus a LogQL query."
reading_time: 5
tags: [loki, logs, structured-metadata, opentelemetry, otlp, logql, observability]
sources:
  - title: "Loki 3.0 release: Bloom filters, native OpenTelemetry support, and more! (Grafana Labs)"
    url: "https://grafana.com/blog/grafana-loki-3-0-release-all-the-new-features/"
  - title: "Grafana Loki 3.3 release: faster query results via Blooms for structured metadata (Grafana Labs)"
    url: "https://grafana.com/blog/grafana-loki-3-3-release-faster-query-results-via-blooms-for-structured-metadata/"
  - title: "Ingesting logs to Loki using OpenTelemetry Collector — Loki docs"
    url: "https://grafana.com/docs/loki/latest/send-data/otel/"
  - title: "What is structured metadata — Loki docs"
    url: "https://grafana.com/docs/loki/latest/get-started/labels/structured-metadata/"
  - title: "Manage bloom filter building and querying (Experimental) — Loki docs"
    url: "https://grafana.com/docs/loki/latest/operations/bloom-filters/"
---

The oldest mistake in Loki is treating it like Elasticsearch and indexing everything. Loki's index is a map of **stream labels** to chunks, and every unique combination of label values is a separate stream. Put `trace_id` or `pod` in a label and you mint a new stream per trace or per pod restart — cardinality explodes, the index bloats, ingesters spend their memory tracking millions of tiny streams, and queries slow to a crawl. The old rule of thumb was blunt: keep labels low-cardinality (`service`, `env`, `cluster`), and shove everything else into the log line as text. **Loki 3.0**, generally available **April 10, 2024**, gave that rule a proper escape hatch.

## The problem: labels are streams, not fields

A stream in Loki is defined entirely by its label set. `{service="checkout", env="prod"}` is one stream; add `trace_id="abc123"` and you have a distinct stream for every trace. High-cardinality fields — trace and span IDs, pod names, request IDs, user IDs — are exactly the things you most want to filter on, and exactly the things you must never make labels. Pre-3.0 you had two bad options: bury them in the log line (queryable only via slow regex/parser scans) or promote them to labels (cardinality disaster).

## Structured metadata: high-cardinality without indexing

**Structured metadata** attaches arbitrary key/value pairs to an individual log line — not to the stream. The values are stored alongside the line in the chunk, are *not* added to the index, and do *not* create new streams. High-cardinality data rides along cheaply, and you query it at read time without a parser.

It requires chunk format V4, i.e. schema `v13` or newer, and is switched on in `limits_config`:

```yaml
limits_config:
  allow_structured_metadata: true
  # requires schema_config with a v13 (or later) period using tsdb
```

In LogQL you filter structured metadata with a label-filter expression right after the stream selector — no `json`/`logfmt` parser needed, because the fields are already attached to the line:

```logql
{service_name="checkout", env="prod"} | trace_id="0242ac120002" | detected_level="error"
```

The stream selector `{service_name=..., env=...}` narrows to a handful of low-cardinality streams; the `| trace_id=...` filter then does a cheap needle lookup over that data without ever having indexed `trace_id`. That is the whole trick: labels stay small, high-cardinality lookups stay fast.

## OTel-native ingestion

The second headline of 3.0 is a **native OTLP endpoint**. Loki now speaks OpenTelemetry directly at `/otlp/v1/logs` — no Loki exporter translation layer in between. Point any OTLP/HTTP client at Loki's base `/otlp` path and it works. Crucially, the mapping was designed around the label-cardinality rule above:

- **Resource attributes → index labels**, but only a curated default set (about 17 keys such as `service.name`, `service.namespace`, `deployment.environment.name`, and the common `k8s.*` names). These identify *where* logs come from — naturally low-cardinality.
- **Everything else** — remaining resource attributes, plus all scope and log attributes — **→ structured metadata**. This is where `trace_id`, `span_id`, and friends land automatically.
- Dots become underscores (`service.name` → `service_name`), since Loki label names allow only `_`.

So an OTel pipeline gets the right physical layout for free: identity in labels, detail in structured metadata. Here is an [Alloy](/articles/observability/2026-07-26-grafana-alloy-collector) config that receives OTLP and forwards it to Loki's native endpoint — the same `otelcol.*` components work in a plain OpenTelemetry Collector:

```alloy
otelcol.receiver.otlp "in" {
  http { endpoint = "0.0.0.0:4318" }
  grpc { endpoint = "0.0.0.0:4317" }
  output { logs = [otelcol.processor.batch.default.input] }
}

otelcol.processor.batch "default" {
  output { logs = [otelcol.exporter.otlphttp.loki.input] }
}

otelcol.exporter.otlphttp "loki" {
  client {
    // Loki serves OTLP at /otlp; the exporter appends /v1/logs
    endpoint = "http://loki:3100/otlp"
  }
}
```

The equivalent stanza in a stock OTel Collector is just `exporters: { otlphttp/loki: { endpoint: http://loki:3100/otlp } }` wired into the `logs` pipeline. Either way, make sure `allow_structured_metadata: true` is set on the Loki side or it will reject the OTLP writes. This dovetails with the OTel Collector material elsewhere in this track.

## Bloom filters: promising, still experimental

The 3.0 announcement also shipped **query acceleration with bloom filters** — a way to skip chunks that cannot contain a given value, so needle-in-a-haystack lookups read far less data (early tests filtered out 70–90% of chunks). Be honest about its maturity: it launched **experimental** in 3.0 aimed at free-text search, and **Loki 3.3** (November 22, 2024) re-pointed it specifically at **structured metadata** lookups — the blooms are "orders of magnitude smaller" that way — and replaced the bloom compactor with new **bloom planner and builder** components. As of the current docs it remains **experimental / public preview**: no SLA, targeted at very high-volume tenants (roughly 75 TB/month and up), and unsupported in single-binary deployments. Treat it as a scaling optimization for large clusters, not a default you turn on.

## Why this matters

Structured metadata plus OTLP-native ingestion means you can finally stop agonizing over which fields are "safe" to keep. Send OpenTelemetry logs straight at Loki, let resource attributes become your small label set, and let everything high-cardinality become structured metadata you can still filter on. The cardinality trap that defined years of Loki operations is now a configuration default rather than a design burden.

**Try next:** Run Loki 3 locally with `allow_structured_metadata: true` and schema `v13`, point the Alloy config above at it, emit a few OTLP logs carrying a `trace_id` attribute, then confirm in `{service_name="..."} | trace_id="..."` that the filter works with no parser — and check `/otlp/v1/logs` is receiving writes via Loki's `/metrics`.
