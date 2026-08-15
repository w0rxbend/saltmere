---
title: "Traces without touching the code: eBPF auto-instrumentation with Beyla/OBI"
date: 2026-07-24
track: observability
summary: "eBPF moves the observer into the kernel, so HTTP and gRPC spans can be reconstructed for a process that cannot be recompiled. Grafana donated Beyla to OpenTelemetry in 2025, where it now lives as OpenTelemetry eBPF Instrumentation (OBI)."
reading_time: 6
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

**Gist.** Distributed tracing conventionally requires editing the application: a software development kit (SDK) is linked in and spans are placed by hand, which is impossible for a vendored binary or a legacy process nobody will rebuild. **extended Berkeley Packet Filter (eBPF)** relocates the observer into the kernel, where probes attached to the system calls a process uses for network input and output reconstruct request spans without changing the application. The cost is a change of vantage point: the kernel observes bytes crossing socket boundaries, so the resulting spans describe **the edges of a service and nothing about its interior**, and the probes require a kernel that supports them plus elevated privileges.

## What the kernel can see and what it cannot

An eBPF program is bytecode loaded into the running kernel and attached to a hook — here, the entry and exit of the system calls a server uses to accept connections and read and write sockets. Because the hook sits below the language runtime, **coverage does not depend on a per-language agent**: a Java virtual machine (JVM) process, a Python interpreter and a Node process are all observed through the same operating system interface. Go is the exception that shows the rule: Beyla instruments Go services with user-space probes (uprobes) attached to functions inside the binary itself, in addition to the kernel-side hooks it uses generally.

From that vantage point the instrumentation parses application-protocol framing out of the byte stream and pairs a request with its response to synthesise a span. The article's subject tool, **Beyla**, does this for HTTP, HTTP/2, gRPC, SQL, Redis, and Kafka. The load-bearing consequence follows from the vantage point rather than from any implementation detail: a span exists **only where a syscall boundary was crossed**. A request that takes 900 ms produces one accurate 900 ms span; it does not decompose into the internal function calls that consumed it, because no syscall separated them.

In May 2025 Grafana donated Beyla to the OpenTelemetry project, where it became **OpenTelemetry eBPF Instrumentation (OBI)**. Beyla continues as Grafana's distribution built on that upstream; the configuration key names differ between the two, so a setting copied from one does not necessarily apply verbatim to the other.

## Attaching to a process

The instrumentation is told which process to watch — by executable name, by listening port, or by Kubernetes selector — and where to send OpenTelemetry Protocol (OTLP) data.

```bash
docker run --rm --pid=host --privileged \
  -e BEYLA_OPEN_PORT=8080 \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317 \
  -e OTEL_SERVICE_NAME=legacy-api \
  grafana/beyla:latest
```

Each flag carries weight. **`--pid=host` places the container in the host process identifier (PID) namespace**, without which the target process is not visible for the probes to attach to; a container isolated in its own PID namespace sees only itself. **`--privileged` supplies the capabilities required to load eBPF bytecode into the kernel**, which an unprivileged container does not have. **`BEYLA_OPEN_PORT=8080` selects the instrumentation target by the port it listens on** rather than by name, which is the discriminator available when the binary's name is unknown or shared. `OTEL_SERVICE_NAME` supplies the service identity that an SDK would otherwise have declared in code — nothing in the byte stream reveals what the service calls itself.

Traffic to the selected process then yields spans carrying method, route, status and latency, with no redeploy of the application. Under Kubernetes the same component runs as a DaemonSet, one instance per node, selecting workloads by namespace and label instead of by port.

For a fleet of ingestion services this is the shortest path to a baseline: rate, errors and duration (RED) metrics plus traces across every service at once, ahead of any effort to add SDK instrumentation.

## The boundaries of the technique

Three limits are worth stating precisely.

**Interior blindness.** As above, spans mark syscall boundaries. Latency attributable to computation between two socket operations appears as one opaque interval.

**Encryption.** A probe reading the socket write sees ciphertext. Beyla addresses this for supported stacks by probing **the cryptographic library's own read and write functions rather than the socket**, so the bytes are seen in plaintext on either side of encryption — which makes decrypted visibility a property of the specific TLS (Transport Layer Security) library the process links against, not a general guarantee.

**Context propagation.** Correlating spans across many hops is less complete than with hand-placed SDK spans. The kernel observes a socket write; it does not by construction know which incoming request caused it.

OpenTelemetry frames eBPF instrumentation and SDKs as complementary rather than competing. eBPF supplies **breadth** — every service covered without code changes — and SDKs supply **depth** — custom spans and business attributes — for the services where internal detail is worth the work of adding them.

## Pitfalls

- **The container starts and reports nothing.** Without `--pid=host` the process to be instrumented lies in a different PID namespace and is not a candidate for probe attachment; the instrumentation has no target rather than a failing one.
- **Loading the eBPF program fails outright.** Attaching probes needs elevated privileges and a kernel that supports the hooks; an unprivileged container or an older kernel produces a load failure, not degraded tracing.
- **Spans appear but payload attributes are absent on HTTPS traffic.** The probe read ciphertext. Decrypted visibility depends on Beyla's TLS support covering the specific stack the application links against; an unsupported stack yields the connection-level span without protocol detail.
- **A latency regression is visible but not localisable.** eBPF spans bound the request at the service edge. Attributing the time to one of several internal code paths requires SDK spans inside the process; no eBPF configuration change produces them.
- **Selecting by port instruments the wrong workload.** `BEYLA_OPEN_PORT` matches whatever is listening on that port on the host. When several processes share a port range or a port is reused after a restart, the identity attached to the spans comes from `OTEL_SERVICE_NAME`, which is static and will mislabel the new occupant.
- **Assuming a trace crosses every hop.** Propagation across multiple services is weaker than with SDK instrumentation, so a trace may terminate at a boundary and appear as several unrelated traces rather than one.
