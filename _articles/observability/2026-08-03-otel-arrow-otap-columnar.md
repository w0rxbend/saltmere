---
title: "OTel Arrow: columnar OTLP reporting 30-70% better compression than OTLP"
date: 2026-08-03
track: observability
summary: "OpenTelemetry Protocol (OTLP) over protobuf is row-oriented, so each span repeats its resource attributes and schema markers, and generic compression works against that layout. OTel Arrow (OTAP) re-encodes batches columnar over a long-lived gRPC stream so dictionaries and prior-batch state carry across requests. This article covers the encoding, the reported compression and CPU figures, and a two-collector configuration using the otelarrow exporter and receiver."
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

**Gist.** The OpenTelemetry Protocol (OTLP) encodes telemetry as row-oriented protobuf, so a batch of spans repeats the same resource attributes, scope, and field tags once per record, and a general-purpose compressor sees that redundancy scattered across the byte stream rather than gathered. OpenTelemetry Protocol with Apache Arrow (OTAP, commonly "OTel Arrow") re-encodes the same batches into Apache Arrow record batches carried over a **long-lived gRPC stream**, so low-cardinality columns collapse under dictionary encoding and schema and dictionary state persist across requests instead of being retransmitted. The cost is CPU: in the production trial cited below, the Arrow exporter consumed more vCPU per byte exported than plain OTLP, which makes the trade worthwhile only where egress is metered more expensively than compute.

## What the layout change buys

Apache Arrow stores data by column rather than by row. When a batch of spans is packed into Arrow record batches, all `service.name` values are contiguous, all timestamps are contiguous, and all trace identifiers are contiguous. Two consequences follow directly from that arrangement.

First, **columns whose values repeat collapse under dictionary encoding**: the distinct value is stored once in a dictionary and each row holds an index into it. Resource attributes are the extreme case — a gateway collector fanning in hundreds of thousands of spans per second from one deployment sees a handful of distinct `service.name` and `k8s.pod.name` values across the entire batch.

Second, **the compressor's window sees long runs of like-typed bytes**. In the row layout, two occurrences of the same string are separated by every other field of a span; in the column layout they are adjacent. zstd therefore has materially more exploitable structure per unit of window.

The larger lever is the transport. OTAP runs over a **long-lived gRPC stream rather than one-shot unary requests**. Exporter and receiver each hold stream state — schemas, dictionaries, and prior batch context — and later requests refer back to it, so a service's resource attributes and schema are transmitted once per stream and subsequent batches are expressed against that established state. This makes stream lifetime a tuning parameter, not an implementation detail: the 2024 production write-up describes longer stream lifetimes as improving compression **with diminishing returns**, and the exporter documents its 30-second `max_stream_lifetime` default on exactly those grounds — compression benefit is limited past that point, and shorter streams make load balancing easier.

The state carried on the stream is also the failure mode. **Stream state is not portable**: a broken connection, a rebalanced load balancer, or a receiver restart discards the accumulated dictionaries, and the next stream re-establishes them from scratch. A pipeline whose streams churn frequently pays the Arrow encoding CPU without accumulating the compression benefit that justifies it.

## Reported figures

Published numbers vary by signal and pipeline shape. The following are ranges reported by the official sources, not guarantees:

- For traces, the 2024 production post reports OTel Arrow reaching reduction factors of **15.6x at a 3.75-second stream lifetime, 17.1x at four minutes, and 17.7x on batches of 4000-5000 spans**, against an OTLP baseline of **12.0-12.2x** on comparable pipelines — summarised in that post as approximately a **30% improvement in compression**.
- For logs and metrics, the same post states that users can "expect 50% to 70% improvement relative to OTLP for similar pipeline configurations".
- The same post describes OTel Arrow output as **15 to 30 times smaller than uncompressed data**.

The CPU trade-off is documented rather than elided. In one production trial the OTel Arrow exporter used **77.0 vCPU** to export **107 GiB/hour**, against OTLP's **53.7 vCPU** for **143 GiB/hour**. Taken at face value, that is roughly 23 additional vCPU in exchange for roughly 36 GiB/hour less exported data; the two configurations are not stated to have carried identical input volumes, so the ratio is indicative rather than a controlled measurement. Whether that exchange is favourable depends on the price of cross-availability-zone or cross-region transfer relative to compute in a given account. **High-volume, egress-metered links between collectors are the case the protocol addresses; a single low-volume collector is not.**

## Stability and current scope

OTel Arrow is **not stable**. The `otelarrow` exporter and receiver both ship in [opentelemetry-collector-contrib](https://github.com/open-telemetry/opentelemetry-collector-contrib) at **beta** stability for traces, metrics, and logs.

The wider protocol and engine work continues to move. The 2026 "Phase 2" post describes the project as an **incubation-stage project** rather than a stabilised platform, and reports that Phase 2 delivered a Rust **OTAP Dataflow Engine** using the columnar form as the in-pipeline representation, with single-core throughput on the order of **2.47M logs/sec on the OTAP path against 121K logs/sec for OTLP**. That post also states that real-world usage of the engine should be limited to controlled experiments and that production workloads are not recommended at this stage. For collector deployments today the usable surface remains the beta exporter/receiver pair; those components extend the core OTLP receiver and exporter settings, so the configuration change is largely substituting `otelarrow` for `otlp`.

## A two-collector configuration

The intended topology is an **edge (agent) collector** exporting to a **gateway collector** over an OTAP stream. Arrow rides gRPC on the same port 4317.

Edge collector — export with `otelarrow`:

```yaml
exporters:
  otelarrow:
    endpoint: gateway.internal:4317
    tls:
      insecure: true            # real certificates in production
    arrow:
      num_streams: 4            # concurrent Arrow streams; default is half the CPU count, minimum 1
      max_stream_lifetime: 30s  # this is also the default
    # gRPC-level zstd is on by default; Arrow-level compression is enabled by default too

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
        # admission control guards against decompression memory growth
    # request_limit_mib (default 128) caps uncompressed in-flight request size
    # waiting_limit_mib (default 32) bounds queued requests

exporters:
  otlp:
    endpoint: backend:4317      # the gateway fans out to the backend as plain OTLP

service:
  pipelines:
    traces:
      receivers: [otelarrow]
      processors: [batch]
      exporters: [otlp]
```

Three properties of this configuration are load-bearing. The exporter **negotiates**: where the receiver does not speak Arrow, it can **downgrade to standard OTLP automatically**, and `arrow.disable_downgrade: true` forbids that — so a mixed-version fleet degrades to OTLP rather than failing, unless downgrade is explicitly disabled. The exporter README documents that **Arrow-level compression is enabled by default because it boosts compression slightly and helps Arrow payloads meet gRPC maximum request size limits**. Finally, an upstream `batch` processor is not optional in practice: larger batches supply more rows per Arrow record batch, and the production post's best reported trace ratio (17.7x) comes from its largest batches, 4000-5000 spans.

The receiver's admission-control settings are the guard against a decoding-side memory hazard. A compressed request expands on decode, so **`request_limit_mib` bounds the uncompressed in-flight size rather than the wire size**, and `waiting_limit_mib` bounds what is queued behind it.

## Pitfalls

- **Short-lived or frequently reset streams erase the compression benefit.** Dictionaries and schema state live on the gRPC stream; a load balancer that rebalances connections aggressively, or an over-tight `max_stream_lifetime`, forces re-establishment and leaves the pipeline paying Arrow's encoding CPU with OTLP-like ratios.
- **Removing the `batch` processor starves the encoder.** Small batches yield few rows per record batch, so dictionary and column encoding have little redundancy to exploit while per-batch overhead stays constant; the reported compression ratios improve with batch size.
- **Silent downgrade masks a misconfiguration.** With downgrade left enabled, a gateway that is not running the `otelarrow` receiver still accepts data over plain OTLP, so the only symptom of the misconfiguration is unchanged egress. `arrow.disable_downgrade: true` converts that into a visible failure.
- **Sizing the receiver by wire bytes underestimates memory.** `request_limit_mib` and `waiting_limit_mib` are stated in uncompressed terms; a limit derived from observed compressed throughput admits far more decoded data than intended.
- **Enabling OTAP on a low-volume link costs more than it saves.** The exporter consumed 77.0 vCPU against OTLP's 53.7 vCPU in the cited trial; where egress is not separately metered, that CPU is unrecovered.
- **Beta stability applies to the collector components, and the wider project is described as incubation-stage.** Configuration surfaces and protocol details remain subject to change, so pinned collector versions and reviewed upgrades are warranted.
