---
title: "Traces without touching the code: eBPF auto-instrumentation with Beyla/OBI"
date: 2026-07-24
track: observability
summary: "eBPF lets the kernel watch your process's network calls from the outside, so you can get HTTP and gRPC spans out of a service you can't or won't recompile. Grafana donated Beyla to OpenTelemetry in 2025, where it now lives as OpenTelemetry eBPF Instrumentation (OBI)."
reading_time: 5
tags: [ebpf, opentelemetry, beyla, obi, tracing, auto-instrumentation]
sources:
  - title: "Grafana Labs — Why we donated Beyla to OpenTelemetry (May 7, 2025)"
    url: "https://grafana.com/blog/2025/05/07/opentelemetry-ebpf-instrumentation-beyla-donation/"
  - title: "Grafana Beyla — GitHub"
    url: "https://github.com/grafana/beyla"
  - title: "OpenTelemetry — eBPF Instrumentation (OBI)"
    url: "https://opentelemetry.io/blog/2025/otel-ebpf-instrumentation/"
  - title: "Monitor your homelab with Beyla, eBPF and OpenTelemetry"
    url: "https://grafana.com/blog/2025/08/22/how-to-monitor-your-homelab-with-beyla-ebpf-and-opentelemetry/"
---

The friction with distributed tracing has always been the same: you have to *instrument the code*. That's fine for the service you own in Scala, awkward for the vendored binary, and a non-starter for the legacy process nobody wants to touch. eBPF sidesteps the whole problem by moving the observer into the kernel — it watches your process make and receive network calls from the outside and reconstructs spans without a single line of application change.

The tool worth knowing is **Beyla**. In May 2025 Grafana donated it to the OpenTelemetry project, where it became **OpenTelemetry eBPF Instrumentation (OBI)**; Beyla now ships as Grafana's thin distribution of that upstream. Either way it auto-instruments HTTP, HTTP/2, gRPC, SQL, Redis, and Kafka regardless of the app's language, because it hooks the syscalls, not the runtime.

## Getting a trace out of a service you didn't write

Point Beyla at a running process (by executable name, port, or Kubernetes selector) and tell it where to send OTLP. That's the whole setup:

```bash
docker run --rm --pid=host --privileged \
  -e BEYLA_OPEN_PORT=8080 \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317 \
  -e OTEL_SERVICE_NAME=legacy-api \
  grafana/beyla:latest
```

`--pid=host` lets it see the target process; `BEYLA_OPEN_PORT=8080` says "instrument whatever is listening on 8080." Hit that service and spans for every HTTP request start flowing to your collector — method, route, status, and latency — with zero redeploy of the app itself. On Kubernetes you run it as a DaemonSet and select workloads by namespace/label instead of a port.

For an IoT backend this is the fast path to a baseline: get RED metrics (rate, errors, duration) and traces across a fleet of ingestion services *today*, before anyone finds time to add SDK instrumentation.

## Know the edges before you lean on it

eBPF sees bytes on sockets, so it's excellent at the *boundaries* of a service and blind to what happens *inside* one. It can tell you a request took 900 ms; it cannot tell you which of three internal functions ate 800 of them. Encrypted traffic needs Beyla's TLS support (it reads at the syscall layer before/after crypto for supported stacks), and deep context propagation across many hops is still less complete than hand-placed spans. It also needs a modern kernel and elevated privileges.

The honest framing — which OpenTelemetry itself now makes — is that eBPF and SDKs are complementary, not rivals: eBPF gives you *breadth* (every service, instantly, for free), SDKs give you *depth* (custom spans, business attributes) where a service earns it. Start broad with eBPF, then add SDK spans to the two or three services that actually need internal detail.

**Try next:** run the container above against any local HTTP service (even a one-file Flask app), send OTLP to a local collector or Grafana Tempo, and view your first no-code flame graph. Then instrument that same app with the OTel SDK and compare — the eBPF spans mark the doors, the SDK spans light up the rooms.
