---
title: "Loki 3: Structured Metadata and OTel-Native Logs"
date: 2026-07-30
track: observability
summary: "Loki 3.0 addressed the label-cardinality trap with structured metadata and added a native OTLP endpoint. Why trace_id must not be a label, how OpenTelemetry attributes map onto Loki, and an export config with the matching LogQL query."
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

**Gist.** Loki's index maps **stream labels** to chunks, so every distinct combination of label values is a separate stream; placing a high-cardinality field such as `trace_id` or `pod` in the label set mints one stream per trace or per pod restart, inflating the index and the ingesters' in-memory stream tracking. **Loki 3.0**, released in **April 2024**, introduced **structured metadata**: key/value pairs attached to an individual log line, stored in the chunk and excluded from the index, filterable at query time without a parser. The cost is that these fields are unindexed — a structured-metadata filter still reads the chunks selected by the stream selector, so the stream selector remains the only mechanism that bounds the volume scanned.

## Labels are streams, not fields

A stream in Loki is defined entirely by its label set. `{service="checkout", env="prod"}` is one stream; adding `trace_id="abc123"` yields a distinct stream for every trace value observed. The fields most useful for filtering — trace and span identifiers, pod names, request identifiers, user identifiers — are precisely the fields whose cardinality makes them unusable as labels.

Before 3.0 the two available placements were both unsatisfactory. Embedding the field in the log line kept the index small but made retrieval depend on a regular-expression or `json`/`logfmt` parser scan over every matching line. Promoting the field to a label made retrieval an index lookup but multiplied stream count by the field's cardinality. The operational rule that followed — keep labels low-cardinality (`service`, `env`, `cluster`) and put everything else in the line text — was a workaround for the absence of a third placement.

## Structured metadata: high-cardinality without indexing

**Structured metadata** attaches arbitrary key/value pairs to a single log line rather than to the stream. The values are stored alongside the line in the chunk, are **not added to the index**, and **do not create new streams**. The invariant that matters: stream count stays a function of the label set alone, independent of how many distinct `trace_id` values are ingested.

The feature requires **chunk format V4**, that is schema **`v13` or newer**, and is enabled in `limits_config`:

```yaml
limits_config:
  allow_structured_metadata: true
  # requires schema_config with a v13 (or later) period using tsdb
```

In LogQL, structured metadata is filtered with a label-filter expression placed **directly after the stream selector**, with no parser stage, because the fields are already attached to the line:

```logql
{service_name="checkout", env="prod"} | trace_id="0242ac120002" | detected_level="error"
```

The stream selector narrows the query to a small number of low-cardinality streams; the `| trace_id=...` filter then performs an equality test over the lines in the chunks those streams cover. The ordering is load-bearing. **A structured-metadata filter does not reduce the set of chunks fetched** — it discards lines after they are read. A query whose stream selector matches a month of every service's logs will read a month of chunks regardless of how selective the `trace_id` filter is.

## OTLP-native ingestion

Loki 3.0 also added a **native OpenTelemetry Protocol (OTLP) endpoint** at `/otlp/v1/logs`, removing the need for a Loki-specific exporter to translate the payload. An OTLP/HTTP client is pointed at the base `/otlp` path and appends `/v1/logs` itself. The documented attribute mapping is:

- **Resource attributes → index labels**, but only a curated default set (including `service.name`, `service.namespace`, `deployment.environment.name`, and common `k8s.*` names). These describe the origin of the logs.
- **All remaining attributes** — the other resource attributes plus every scope attribute and log attribute — **→ structured metadata**. `trace_id` and `span_id` land here without configuration.
- **Dots become underscores** (`service.name` → `service_name`); Loki label names cannot contain dots.

The result is that an OpenTelemetry pipeline produces the physical layout described above by default: identity in labels, detail in structured metadata. The following [Alloy](/articles/observability/2026-07-26-grafana-alloy-collector) configuration receives OTLP and forwards it to Loki's native endpoint; the same `otelcol.*` components exist in a stock OpenTelemetry Collector:

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

The equivalent in a stock Collector is `exporters: { otlphttp/loki: { endpoint: http://loki:3100/otlp } }` wired into the `logs` pipeline. In both cases `allow_structured_metadata: true` must be set on the Loki side; without it Loki rejects the OTLP writes, since the default mapping routes every non-curated attribute into structured metadata.

## Bloom filters: experimental chunk skipping

The 3.0 announcement also included **query acceleration with bloom filters**, which allows a query to skip chunks that cannot contain a given value and therefore reduces the data read for needle-in-a-haystack lookups. Its maturity is limited. It shipped **experimental** in 3.0, aimed at free-text search. **Loki 3.3** (**November 2024**) re-targeted it specifically at **structured-metadata lookups**, where the blooms are described as "orders of magnitude smaller", and replaced the bloom compactor with separate **bloom planner and bloom builder** components. The documentation still classifies it as **experimental / public preview**: no service-level agreement, targeted at **very high-volume tenants**, and unsupported in single-binary deployments.

The interaction with the previous section is the point. Bloom filters are the mechanism that would let a structured-metadata filter prune chunks rather than only discard lines — but that mechanism is preview-only and unavailable to small and single-binary deployments, so on most clusters the stream selector remains the sole bound on bytes read.

## Consequence for schema design

Structured metadata plus OTLP-native ingestion changes the placement decision from a design burden into a configuration default: resource attributes form the small label set, everything of higher cardinality becomes structured metadata that is still filterable without a parser. What does not change is that the stream selector still determines how much data a query touches. Labels must remain chosen for their partitioning value over the query workload, not merely for being low-cardinality.

**Try next:** run Loki 3 with `allow_structured_metadata: true` and schema `v13`, point the Alloy configuration above at it, emit OTLP logs carrying a `trace_id` attribute, and confirm that `{service_name="..."} | trace_id="..."` matches with no parser stage. Compare the bytes-processed figure reported for that query against one whose stream selector omits `service_name` to observe that the structured-metadata filter does not reduce the scan.

## Pitfalls

- **Structured metadata written to a schema older than `v13` is rejected at ingest.** The feature needs chunk format V4; a `schema_config` whose active period predates `v13` fails the write rather than silently degrading.
- **A selective `| trace_id=...` filter over a broad stream selector still reads every chunk in range.** The filter is applied after decompression, so query cost tracks the stream selector and time range, not the filter's selectivity.
- **Enabling the OTLP endpoint without `allow_structured_metadata: true` causes OTLP writes to be rejected.** The default attribute mapping routes non-curated attributes into structured metadata, so the endpoint cannot function with the feature disabled.
- **Attribute names with dots do not match label matchers verbatim.** `service.name` is stored as `service_name`; a LogQL selector written with the OpenTelemetry spelling matches nothing.
- **Adding a resource attribute outside the curated default set does not create a label.** It becomes structured metadata, so a dashboard that groups by it as a stream label returns no series.
- **Enabling bloom filters on a single-binary deployment is unsupported.** The planner and builder are separate components; the acceleration is documented for large multi-component clusters and carries no service-level agreement.
