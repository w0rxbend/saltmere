---
title: "The OpenTelemetry Profiling Signal: the Fourth Pillar Enters Alpha"
date: 2026-07-31
track: observability
summary: "Continuous profiling became a first-class OpenTelemetry signal alongside traces, metrics, and logs on 26 March 2026. This article covers the OTLP profiles data model, the span-linkage mechanism, the eBPF profiler's documented status, and the Collector's profiles pipeline, as of mid-2026."
reading_time: 6
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

**Gist.** A distributed trace localises latency to a span but not to the code executing inside it, so the last hop of a performance investigation has historically been made with a separate, uncorrelated profiler. OpenTelemetry's fourth signal — **profiles**, in **public Alpha since 26 March 2026** — carries sampled call stacks in an OpenTelemetry Protocol (OTLP) representation that shares the resource and scope structure of the other three signals and can carry `trace_id` and `span_id` on individual samples, making the pivot from span to stack a join rather than a manual correlation. The cost is that Alpha status means the wire format and application programming interfaces (APIs) are declared usable for experimentation and explicitly **not** ready for critical production workloads, so anything built on the format today may need to change.

## What continuous profiling supplies that traces do not

A span records that an operation took a given wall-clock duration. A profile records **which call stacks accumulated that time**, obtained by sampling stacks across a fleet continuously at low overhead. The signal is rendered as flame graphs over several distinct profile types:

- **On-CPU** — functions consuming processor time.
- **Off-CPU** — where threads block or wait: locks, input/output, syscalls.
- **Heap / in-use memory** — allocations still resident.
- **Allocation** — the code paths responsible for the most allocations.

Flame graphs predate this work by many years and are available from standalone tools. The property specific to the OpenTelemetry signal is **correlation**: the profile carries the same resource attributes (`service.name`, `k8s.pod.name`, and the rest) as the traces, metrics, and logs emitted beside it, so a pivot from a slow span or a memory alert to the responsible stack does not require reconciling two independent identity schemes.

## The OTLP profiles data model

The data model derives from Google's **pprof** format but is not pprof carried verbatim over the wire. OTLP profiles are a redesigned representation intended to remain **convertible to and from pprof**, while addressing pprof's encoding inefficiency at fleet scale:

- **Deduplicated stacks.** Each unique call stack is stored once rather than repeated for every sample referencing it. Sampling at fleet scale produces heavily repeated stacks, so the saving grows with the sample count rather than being a fixed overhead reduction.
- **Shared string and dictionary tables** spanning the payload, so repeated function, file and label strings are transmitted once. No published benchmark in the cited material fixes the resulting size reduction as a single figure.
- **The same resource and scope structure as the other signals.** This is what makes correlation mechanical: identity is not re-derived per signal, so a profile and a span from the same process agree on service and pod by construction.

The format is defined in the OTLP protocol-buffer definitions, and the semantic conventions specify profile types and units so that a backend interprets, for example, a samples/count on-CPU profile identically regardless of which agent produced it.

## The linkage mechanism between profiles and spans

Correlation operates at two granularities, and the distinction determines what a query can answer.

**Sample-level linkage.** When the runtime producing the profile knows the active request context, individual samples carry **`trace_id` and `span_id`** attributes. A backend can then join a flame graph to the exact span executing at sample time, which supports navigation in both directions: from a slow span to the code running during it, and from a hot function to the requests that exercised it.

**Resource-level linkage.** When request context is unavailable — the case for whole-system eBPF sampling, which observes native libraries and kernel frames belonging to no traced request — samples carry no trace identifiers. Correlation then falls back to **shared resource attributes**: service, host, and, where the Collector's `k8sattributesprocessor` has enriched the data, Kubernetes pod and deployment metadata. The failure mode to anticipate is a silent degradation: a pipeline missing `k8sattributes` still emits valid profiles, but a stack observed on a node cannot be attributed to a workload, and the resulting flame graph is unattributable rather than absent.

## The eBPF profiler, and the project it is not

Two distinct extended Berkeley Packet Filter (eBPF) projects exist in the OpenTelemetry ecosystem and are frequently conflated.

**The OpenTelemetry eBPF profiler** (`opentelemetry-ebpf-profiler`) descends from **Elastic's Universal Profiling agent**, donated to OpenTelemetry in 2024. It is a whole-system, multi-runtime continuous profiler: one agent per node samples the processes running on that node across several runtimes — among them Go, the Java Virtual Machine (JVM), Python, Ruby and Node/V8 — together with third-party libraries and kernel frames, without per-application instrumentation. As of the Alpha it performs **on-host symbolization for Go, JIT (runtime-compiled) and interpreted stacks**. **Native frames are symbolized later**, off-host or in the backend, and **the protocol for uploading symbols to a backend is still being specified**. The practical consequence is that a native frame can arrive at a backend as an address without a resolved name until that path is complete.

**OBI (OpenTelemetry eBPF Instrumentation)** is a separate project, the former **Grafana Beyla**, donated by Grafana Labs. OBI produces **traces and metrics** from eBPF. It is not a profiler and does not emit the profiles signal. Deploying OBI in the expectation of flame graphs yields traces and metrics instead.

## Where the Collector fits

The Collector gained a **`profiles` pipeline type** alongside `traces`, `metrics`, and `logs`. The OTLP receiver and exporter handle profiles, so a Collector can receive and forward them as it does any other signal. The eBPF profiler is distributed as its own **Collector distribution**, in which the profiler runs **as a receiver inside the Collector**, so the existing processors — batching, `k8sattributes`, OpenTelemetry Transformation Language (OTTL) transforms — and exporters apply unchanged.

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

Capturing stacks from a whole node requires the eBPF profiler distribution running privileged and in the host process-identifier (PID) namespace, without which it observes only its own container's processes:

```bash
docker run --privileged --pid=host \
  ghcr.io/open-telemetry/opentelemetry-collector-releases/opentelemetry-collector-ebpf-profiler:<version>
```

## Documented status, mid-2026

- **Profiling signal and OTLP profiles format:** public Alpha since 26 March 2026. Usable for experimentation; the format may still change; declared unsuitable for critical production workloads.
- **eBPF profiler:** the most mature component, with lineage from Elastic's production agent, but native symbolization and the symbol-upload protocol are still landing. Distributed via the `opentelemetry-collector-ebpf-profiler` images.
- **Language software development kits (SDKs):** profiling support per language is early and uneven. The eBPF agent requires no code changes and is therefore the lower-friction path at present.
- **Collector:** the `profiles` pipeline and OTLP receiver/exporter support are shipping; other components are still acquiring profiles support, so a processor that works in a traces pipeline is not guaranteed to work in a profiles one.

The signal supports piloting — validating correlation end to end and measuring agent overhead on representative nodes — ahead of any dependency on it for a service-level objective.

## Pitfalls

- **Assuming OBI emits profiles.** Deploying `opentelemetry-ebpf-instrumentation` produces traces and metrics; the profiles pipeline stays empty because the two eBPF projects are distinct and only `opentelemetry-ebpf-profiler` emits the profiles signal.
- **Unresolved native frames.** Flame graphs show raw addresses in place of native function names, because on-host symbolization covers Go, JIT, and interpreted stacks only, and the symbol-upload protocol to the backend is still being specified.
- **Running the profiler without `--pid=host`.** The agent starts and reports healthy, but the flame graph contains only the profiler's own container, since without the host PID namespace no other process is visible to it.
- **Omitting `k8sattributes` from the profiles pipeline.** Profiles arrive and render, but cannot be joined to a deployment or pod, because Kubernetes metadata is added by that processor rather than by the profiler.
- **Persisting stored profiles against the Alpha format.** Data written today may not be readable by a later component version, because Alpha status carries no wire-format stability guarantee.
- **Expecting sample-level span linkage from whole-system sampling.** Samples taken outside a known request context carry no `trace_id` or `span_id`, so the join degrades to resource attributes and per-span attribution is unavailable.
