---
title: "OTel Arrow: columnar OTLP that cuts collector-to-collector bandwidth by 30-70%"
date: 2026-08-03
track: observability
summary: "OTLP over protobuf is row-oriented, so it repeats the same resource attributes and schema on every span, and gzip can only do so much about it. OTel Arrow (OTAP) re-encodes batches columnar over a long-lived gRPC stream, letting zstd exploit dictionaries and cross-request state. Here's why it compresses better, the phased/stream design, and a working two-collector config using the otelarrow exporter and receiver."
reading_time: 6
tags: [opentelemetry, otel-arrow, otap, otlp, collector, columnar, zstd]
sources:
  - title: "open-telemetry/otel-arrow — Protocol and libraries for OpenTelemetry over Apache Arrow"
    url: "https://github.com/open-telemetry/otel-arrow"
  - title: "OpenTelemetry Protocol with Apache Arrow in Production — OpenTelemetry blog (2024)"
    url: "https://opentelemetry.io/blog/2024/otel-arrow-production/"
  - title: "OTel-Arrow Phase 2: From Efficient Transport to Efficient Telemetry Pipelines — OpenTelemetry blog (2026)"
    url: "https://opentelemetry.io/blog/2026/otel-arrow-phase-2/"
  - title: "otelarrow exporter README — opentelemetry-collector-contrib"
    url: "https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/otelarrowexporter/README.md"
  - title: "otelarrow receiver README — opentelemetry-collector-contrib"
    url: "https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/receiver/otelarrowreceiver/README.md"
---

Standard OTLP is a protobuf message, and protobuf is row-oriented: every span carries its own copy of the resource attributes, scope, and field tags, laid out one record after another. That's fine at low volume, but a gateway collector fanning in hundreds of thousands of spans a second is shipping the same `service.name`, `k8s.pod.name`, and schema markers over and over. Generic compression (gzip, and even zstd on a single request) claws some of that back, but it's working against a layout that scatters the redundancy across the byte stream. OTel Arrow — formally the OpenTelemetry Protocol with Apache Arrow, or OTAP — changes the layout instead of just compressing harder. It's a columnar re-encoding of OTLP designed for exactly one job: moving telemetry between two collectors over the wire as cheaply as possible.

## Why columnar wins here

Apache Arrow stores data by column, not by row. When you pack a batch of spans into Arrow record batches, all the `service.name` values sit contiguously, all the timestamps sit contiguously, all the trace IDs sit contiguously. Two things follow. First, columns of repeated or low-cardinality values (resource attributes are the classic case) collapse under dictionary encoding — the value is stored once and referenced by index. Second, a compressor now sees long runs of similar bytes instead of the same string sprinkled between unrelated fields, so zstd gets far more to work with.

The bigger lever is that OTAP runs over a **long-lived gRPC stream**, not one-shot unary requests. The exporter and receiver each hold stream state — schemas, dictionaries, and prior batch context — that later requests refer back to. So the resource attributes and schema for a service get transmitted essentially once per stream, and subsequent batches send deltas against that established state. The OpenTelemetry production write-up describes longer stream lifetimes as improving compression with diminishing returns, which is why `max_stream_lifetime` is a tunable rather than "forever."

## The numbers, honestly

Reported figures vary by signal and pipeline, so treat these as ranges from the official sources, not guarantees:

- For traces, the 2024 production post reports OTel Arrow reaching a **16.4-17.7x** compression reduction factor versus **11.9-12.2x** for OTLP on similar pipelines — roughly a **30% improvement**, translating to something like **30-50% less network bandwidth**.
- For logs and metrics, the same post says users can "expect 50% to 70% improvement relative to OTLP for similar pipeline configurations."
- OTAP's own encoding reaches roughly **15x to 30x of uncompressed size** on internal telemetry.

There is a CPU trade-off, and the docs are refreshingly candid about it. In one production trial the OTel Arrow exporter used **77.0 vCPU** to export **107 GiB/hour**, versus OTLP's **53.7 vCPU** for **143 GiB/hour** — you spend ~23 extra vCPU/hour to save ~36 GiB/hour of egress. Whether that's a win depends on your cross-AZ or cross-region data transfer bill relative to compute. High-volume, egress-metered pipelines are the sweet spot; a low-volume single collector is not.

## Current status

OTel Arrow is **not stable yet**. The `otelarrow` exporter and receiver both ship in [opentelemetry-collector-contrib](https://github.com/open-telemetry/opentelemetry-collector-contrib) at **beta** stability for traces, metrics, and logs, and have since around collector v0.104.0 (July 2024). The broader protocol/engine work is still moving: the June 2026 "Phase 2" blog post describes the project as an "incubation-stage project" rather than a production-stabilized platform, and Phase 2 delivered a Rust **OTAP Dataflow Engine** that uses the columnar form as the in-pipeline representation (reporting single-core throughput on the order of 2.47M logs/sec on the OTAP path versus 121K logs/sec for OTLP). For today's collector deployments, the practical surface is the beta exporter/receiver pair — the components extend the core OTLP receiver and exporter settings, so you largely swap `otlp` for `otelarrow`.

## A two-collector config

The intended topology is an **edge (agent) collector** exporting to a **gateway collector** over an OTAP stream. Arrow rides gRPC on the same 4317 port.

Edge collector — export with `otelarrow`:

```yaml
exporters:
  otelarrow:
    endpoint: gateway.internal:4317
    tls:
      insecure: true            # use real certs in production
    arrow:
      num_streams: 4            # concurrent Arrow streams; default max(1, NumCPU()/2)
      max_stream_lifetime: 30s  # bound stream age; balances compression vs churn
    # zstd is applied at the Arrow level by default

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]       # batching feeds Arrow larger record batches
      exporters: [otelarrow]
```

Gateway collector — receive with `otelarrow`:

```yaml
receivers:
  otelarrow:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
        # admission control guards against decompression memory blowups
    # request_limit_mib (default 128) caps uncompressed in-flight request size
    # waiting_limit_mib (default 32) bounds queued requests

exporters:
  otlp:
    endpoint: backend:4317      # gateway can fan out to your backend as plain OTLP

service:
  pipelines:
    traces:
      receivers: [otelarrow]
      processors: [batch]
      exporters: [otlp]
```

Two things worth knowing. The exporter negotiates: if the receiver doesn't speak Arrow it can **downgrade** to standard OTLP automatically (set `arrow.disable_downgrade: true` to forbid that), so a mixed fleet degrades gracefully rather than failing. And because Arrow compression can produce payloads that bump against gRPC's default max request size, zstd at the Arrow level is enabled by default partly to keep requests under that limit — leave it on. Keep a `batch` processor upstream: larger batches give Arrow more rows per record batch, and the production data shows CPU per log dropping sharply as batch size grows.

**Try next:** stand up two contrib collectors locally, point one `otelarrow` exporter at the other, and compare exported bytes/sec against the same pipeline running plain `otlp` with gzip — the delta is your real-world bandwidth saving before you commit it to a cross-AZ hop.
