---
title: "The OpenTelemetry Profiling Signal: the Fourth Pillar Goes Alpha"
date: 2026-07-31
track: observability
summary: "Continuous profiling is now a first-class OpenTelemetry signal alongside traces, metrics, and logs. Here's the OTLP profiles data model, how it links to spans, the eBPF profiler's real status, and where the Collector fits — as of mid-2026."
reading_time: 5
tags: [opentelemetry, profiling, ebpf, observability, otlp, continuous-profiling]
sources:
  - title: "OpenTelemetry Profiles Enters Public Alpha (OTel blog)"
    url: "https://opentelemetry.io/blog/2026/profiles-alpha/"
  - title: "Profiles signal concept docs (OpenTelemetry)"
    url: "https://opentelemetry.io/docs/concepts/signals/profiles/"
  - title: "OpenTelemetry Profiling goes Alpha (Polar Signals)"
    url: "https://www.polarsignals.com/blog/posts/2026/03/26/opentelemetry-profiling-goes-alpha"
  - title: "OTel Profiles Signal Enters Alpha (Elastic Observability Labs)"
    url: "https://www.elastic.co/observability-labs/blog/otel-profiling-alpha"
  - title: "Introducing OpenTelemetry eBPF Instrumentation: donating Grafana Beyla (Grafana Labs)"
    url: "https://grafana.com/blog/opentelemetry-ebpf-instrumentation-beyla-donation/"
---

For years OpenTelemetry had three signals: traces, metrics, and logs. The long-promised fourth — **profiles** — stopped being a proposal on **March 26, 2026**, when the profiling signal officially entered **public Alpha**. This is a status update worth reading carefully, because "Alpha" here is load-bearing: it means the wire format and APIs are usable and stable enough for experimentation, but explicitly **not** ready for critical production workloads.

## What continuous profiling actually buys you

A trace tells you a span took 800ms. A profile tells you *which lines of code* burned those milliseconds. Continuous profiling samples your fleet's call stacks at low overhead, constantly, so you can render flame graphs across several dimensions:

- **On-CPU** — functions consuming processor time.
- **Off-CPU** — where threads block or wait (locks, I/O, syscalls).
- **Heap / in-use memory** — allocations still resident.
- **Allocation** — the code paths responsible for the most allocations.

The payoff is not the flame graph in isolation — plenty of tools have shipped those. It's *correlation*: pivoting from a slow span or a memory alert straight to the responsible stack, with the same resource attributes (service, pod, deployment) attached to all four signals.

## The OTLP profiles data model

The data model is built on Google's **pprof** format, but it is not just pprof-over-the-wire. OTLP profiles are a redesigned representation that is **round-trip compatible with pprof with no loss of information**, while fixing pprof's efficiency problems for fleet-scale telemetry:

- **Deduplicated stacks** — each unique call stack is stored once, not repeated per sample.
- **A string/dictionary table** shared across the payload, yielding roughly **40% smaller wire size** than naive pprof encoding.
- The same **resource and scope** structure as the other signals, so a profile carries the identical `service.name`, `k8s.pod.name`, etc.

The format lives in the OTLP proto definition, and the semantic conventions cover profile types and units so backends can interpret a "samples/count on-CPU" profile the same way everywhere.

## How profiles link to traces and spans

Correlation is the whole point, and it works through attributes on the sample. When your runtime knows the active request context, samples carry **`trace_id` and `span_id`**, letting a backend join a flame graph to the exact span that was executing. That gives you bidirectional navigation: from a slow span → the code running during it, and from a hot function → the requests that hit it. When context isn't available (e.g. whole-system eBPF sampling of a native library), profiles still correlate by shared resource attributes — service, host, and, via the Collector's `k8sattributesprocessor`, Kubernetes pod/deployment metadata.

## The eBPF profiler — and what it is *not*

This is where precision matters, because two different eBPF projects live in the OTel ecosystem and they are frequently conflated.

**The OTel eBPF profiler** (`opentelemetry-ebpf-profiler`) descends from **Elastic's Universal Profiling agent**, which Elastic donated to OpenTelemetry in 2024. It's a whole-system, multi-runtime continuous profiler: one agent per node samples *everything* — Go, JVM, Python, Node/V8, .NET, Ruby, plus third-party libraries and kernel frames — with minimal overhead and no per-app instrumentation. As of the alpha it does **on-host symbolization for Go, JIT, and interpreted stacks**; native frames are symbolized later, off-host or in the backend, and a protocol for uploading symbols to a backend is still being specified.

**OBI (OpenTelemetry eBPF Instrumentation)** is a *separate* project — the former **Grafana Beyla**, donated by Grafana Labs. OBI produces **traces and metrics** via eBPF; it is **not** the profiler. Same "eBPF in OTel" neighborhood, different tool and different signal. Don't wire up OBI expecting flame graphs.

## Where the Collector fits

The Collector gained a **`profiles` pipeline type** alongside `traces`/`metrics`/`logs`. The OTLP receiver and exporter handle profiles, so a Collector can receive OTLP profiles and forward them like any other signal. The eBPF profiler ships as its own **Collector distribution** — it runs *as a receiver inside the Collector*, then reuses your existing processors (batching, `k8sattributes`, OTTL transforms) and exporters.

A minimal profiles pipeline forwarding OTLP profiles to a backend:

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317

processors:
  k8sattributes: {}
  batch: {}

exporters:
  otlp:
    endpoint: backend.example.com:4317

service:
  pipelines:
    profiles:                     # the fourth pipeline type
      receivers: [otlp]
      processors: [k8sattributes, batch]
      exporters: [otlp]
```

To capture stacks from a whole node, run the eBPF profiler distribution (privileged, host PID namespace so it can see every process):

```bash
docker run --privileged --pid=host \
  ghcr.io/open-telemetry/opentelemetry-collector-releases/opentelemetry-collector-ebpf-profiler:0.148.0
```

## Honest status, mid-2026

- **Profiling signal / OTLP profiles format:** public **Alpha** since March 26, 2026. Usable for experimentation; format may still change; not for critical production.
- **eBPF profiler:** the most mature piece — production-grade lineage from Elastic — but native symbolization and symbol-upload protocols are still landing. Distributed via the `opentelemetry-collector-ebpf-profiler` images.
- **Language SDKs:** profiling support in the per-language SDKs is early and uneven; the eBPF agent is the practical on-ramp today because it needs no code changes.
- **Collector:** the `profiles` pipeline and OTLP receiver/exporter support are real and shipping, but expect components to still be gaining profiles support.

Treat it as a signal you can start *piloting* now — validate correlation, get a feel for overhead — not one you bet an SLO on yet.

**Try next:** Spin up the `opentelemetry-collector-ebpf-profiler:0.148.0` image on a single dev node running a Go service, point its OTLP output at a profiles-capable backend (Grafana/Pyroscope, Elastic, or Polar Signals), generate load, and confirm you can pivot from a hot function in the flame graph back to the `service.name` and pod that produced it.
