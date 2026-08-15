---
title: "Coroot: a whole observability platform built on eBPF"
date: 2026-08-13
track: observability
summary: "Coroot uses extended Berkeley Packet Filter (eBPF) programs on syscall boundaries to derive a service map, per-dependency RED metrics, and SLO-driven inspections without application code changes. It is an Apache-2.0 platform — server, node agents, Prometheus, ClickHouse — rather than an instrumentation agent like Beyla/OBI. This article covers the mechanism, the deployment shape, and the limits of the syscall vantage point."
reading_time: 6
tags: [coroot, ebpf, zero-instrumentation, service-map, slo]
sources:
  - title: "coroot/coroot (GitHub) — releases"
    url: "https://github.com/coroot/coroot"
  - title: "Coroot documentation — Installation"
    url: "https://docs.coroot.com/installation/"
  - title: "Coroot documentation — Quick start"
    url: "https://docs.coroot.com/quick-start/"
  - title: "Coroot Pricing & Review: eBPF Observability Tool — CubeAPM"
    url: "https://cubeapm.com/blog/coroot-pricing-review/"
  - title: "Top eBPF Observability Tools — Metoro"
    url: "https://metoro.io/blog/top-ebpf-observability-tools"
---

**Gist.** Application-level telemetry requires each service to carry an instrumentation library, so coverage is exactly as complete as the deployment discipline behind it. Coroot removes that dependency by attaching extended Berkeley Packet Filter (eBPF) programs to the socket syscalls every process already issues, deriving the dependency graph and per-dependency latency and error rates from the kernel's view. The cost is a vantage point that stops at the process boundary: nothing inside a request — the slow function, the business identifier, the propagated trace context across a queue — is visible, and the agent must run privileged on every host.

The [Beyla/OBI article](/articles/observability/2026-07-24-ebpf-zero-code-instrumentation-beyla-obi) treated eBPF as an instrumentation technique: hook the kernel, emit OpenTelemetry (OTel) spans, feed an existing pipeline. Coroot takes the same vantage point and builds the whole platform on it — collection, storage, service map, service level objective (SLO) tracking and root-cause inspection in one deployable unit. It is Apache-2.0 licensed and under active development. It appears on the Cloud Native Computing Foundation (CNCF) landscape but is **not a CNCF-hosted project**.

## How the service map is derived from syscalls

Coroot's **node-agent** runs once per host — a DaemonSet under Kubernetes — and attaches eBPF programs to the syscalls that constitute network I/O: `connect`, `accept`, and reads and writes on sockets. Two facts fall out of those hooks without any cooperation from the workload.

First, **the peer graph**. A `connect` observed in a process gives the initiating process identifier and container alongside the destination address; an `accept` gives the receiving side. Correlating the two ends across hosts yields edges that are *observed* rather than *declared*. Nothing in the graph depends on a service registry, a mesh configuration or a developer's memory, so the undocumented edges appear on equal footing with the intended ones: a sidecar polling a metadata endpoint, a service still receiving traffic after being declared deprecated, a pod issuing an unexpected volume of DNS queries.

Second, **protocol semantics**. The socket boundary is where payload bytes cross into the kernel, so the agent can parse the application protocol in flight. Coroot parses HTTP, gRPC, Postgres, MySQL, Redis, Mongo, Kafka, memcached and DNS. Parsing turns a byte counter into request-level metrics: **request rate, error rate and a latency histogram per (container, upstream dependency) pair** — the RED signals, attributed to the edge rather than to the service as a whole. That per-edge attribution is what makes the map diagnostic instead of decorative: a latency increase is localised to a specific dependency without a distributed trace.

The invariant behind the approach is that **every network interaction must traverse a syscall**, so coverage is complete for anything that talks over a socket. The corresponding limit is equally structural: work that never crosses that boundary leaves no trace at all.

The minimum requirement is **a Linux kernel new enough to load the agent's eBPF programs, with BPF Type Format (BTF) available**; the documented floor is stated per release rather than fixed across versions. The deployed platform comprises the Coroot server (user interface and inspections), the node-agents, a cluster-agent, **Prometheus** for metrics and **ClickHouse** for logs, traces and profiles. Both stores ship bundled and both are pluggable, so an existing Prometheus can back the installation instead.

## Inspections: SLO objectives in place of independent alert rules

The layer above collection is where Coroot differs from an instrumentation agent. Rather than a large rule set evaluated independently, it ships a fixed set of **predefined inspections organised around SLOs**. Every application receives availability and latency objectives — defaults, overridable per service — and inspections are evaluated in service of those objectives rather than as standalone conditions.

When an objective's error budget burns, the platform walks its model of the affected service and presents correlated evidence in one view: container terminations from out-of-memory (OOM) kills, CPU throttling, latency on a Postgres dependency, DNS failures, or a recent deployment. The practical effect is that one incident produces one correlated report instead of a page per firing rule.

Coroot also tracks Kubernetes deployments and **compares each release against the preceding release's baseline**, which surfaces regressions whose only signal is a shift in distribution after a rollout. The burn-rate mechanics underlying the alerting are those described in the [burn-rate article](/articles/observability/2026-07-27-slo-burn-rate-alerts); Coroot supplies them pre-configured.

## Deployment

On Kubernetes the documented route is the operator:

```bash
helm repo add coroot https://coroot.github.io/helm-charts
helm repo update coroot

# Operator, then the Community Edition
helm install -n coroot --create-namespace coroot-operator coroot/coroot-operator
helm install -n coroot coroot coroot/coroot-ce

kubectl port-forward -n coroot service/coroot-coroot 8080:8080
```

For a single Docker host:

```bash
curl -fsS https://raw.githubusercontent.com/coroot/coroot/main/deploy/docker-compose.yaml \
  | docker compose -f - up -d
```

The compose file starts five containers: coroot, node-agent, cluster-agent, Prometheus and ClickHouse. The interface is served on port 8080, and the map populates as traffic is observed — there is no discovery phase to wait out beyond the arrival of requests. The node-agent loads eBPF programs and therefore **requires privileged access**, which is the constraint that restrictive Pod Security Standards will block.

## The ceiling of the syscall vantage point

- **No intra-service visibility.** eBPF observes requests entering and leaving a process. It cannot attribute latency to a function, recover business context, or attach an application-defined attribute such as a device identifier. Code-level OTel instrumentation remains the only source for that.
- **Weaker trace semantics.** Coroot captures and correlates spans derived from network activity, and ingests OTel traces where they exist, but network-derived spans lack the deliberate, attribute-rich structure a developer writes. Context propagation across asynchronous hops — queues, MQTT — is largely invisible, because the causal link between producer and consumer is carried in the message payload rather than in a socket pairing.
- **Encryption is a partial blind spot.** The agent hooks common Transport Layer Security (TLS) libraries, including OpenSSL and Go's `crypto/tls`, to observe plaintext at the library boundary. Where that hook does not apply — mutual TLS terminated in a service-mesh sidecar, or a runtime with its own TLS implementation — protocol parsing degrades to opaque TCP byte counts.
- **Linux only**, on a kernel that supports the agent's eBPF programs. There is no browser or mobile real user monitoring (RUM).

Against **Beyla/OBI**: Beyla is a component — an agent emitting OpenTelemetry Protocol (OTLP) data into whatever backend is already deployed, such as Tempo or ClickStack. Coroot is a complete platform: agents, storage, interface and inspection logic. Beyla fits an existing pipeline that needs eBPF as one additional source; Coroot fits an environment with no pipeline at all. The two approaches also compose with conventional instrumentation — Coroot as the always-on base layer covering every process, OTel software development kits (SDKs) added to the services where business-level attributes are required.

## Pitfalls

- **Privileged node-agent under restrictive Pod Security Standards.** The DaemonSet fails to start and no data appears, because loading eBPF programs requires privileges the `restricted` profile denies.
- **Kernel below the agent's documented floor, or a kernel built without BTF.** The agent starts but produces no protocol metrics; the graph stays empty or degenerates to raw connection counts.
- **Sidecar-terminated mutual TLS.** Edges remain visible but their per-dependency latency and error metrics vanish, because the payload the agent sees at the socket is ciphertext and no hooked library boundary exposes the plaintext.
- **Expecting queue-mediated causality on the map.** A producer and a consumer connected only through Kafka or MQTT appear as two separate edges to the broker rather than one request path, because the correlation lives in the message rather than in a socket pair.
- **Treating total coverage as total depth.** Every service appears on the map, which makes a missing root cause look like a Coroot defect rather than what it is: the requested detail lives inside the process, below the syscall boundary.
- **Assuming the bundled stores are sized for the workload.** The default Prometheus and ClickHouse are deployed for a first installation; retention and capacity for a production cluster are a separate configuration decision.
