---
title: "Coroot: a whole observability platform from eBPF, no code changes"
date: 2026-08-13
track: observability
summary: "Coroot uses eBPF to build a full service map, RED metrics, and SLO-based inspections for every service on a host — without touching a line of application code. It's an Apache-2.0 platform (server + node agents + Prometheus + ClickHouse), not just an instrumentation agent like Beyla/OBI. Here's how it works, how to install it, and what eBPF fundamentally cannot see."
reading_time: 5
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

The [Beyla/OBI article](/articles/observability/2026-07-24-ebpf-zero-code-instrumentation-beyla-obi) covered eBPF as an *instrumentation* trick: hook the kernel, emit OTel spans, feed your existing pipeline. **Coroot** takes the same kernel-level vantage point and builds the *entire platform* on it — collection, storage, service map, SLO tracking, and root-cause analysis in one deployable unit. It's open source (Apache-2.0), actively developed (**v1.22.2**, June 2026, ~7.8k GitHub stars), listed on the CNCF landscape though — worth being precise — it is not a CNCF-hosted project. For a cluster you've never instrumented, it's the fastest route I know from "nothing" to "annotated service map with latency SLOs."

## How the service map falls out of the kernel

Coroot's **node-agent** runs on every host (DaemonSet on Kubernetes) and attaches eBPF programs to the syscalls every process already makes: `connect`, `accept`, reads and writes on sockets. From that it learns, per process and container, who talks to whom — and because the kernel sees payloads at the socket boundary, the agent parses application protocols in flight: HTTP, gRPC, Postgres, MySQL, Redis, Mongo, Kafka, memcached, DNS. Each container gets metrics like requests per second, errors, and latency histograms *per upstream dependency*, with zero SDKs and zero restarts.

Stitch that together across nodes and you get a service map that is *observed*, not declared — including the dependencies nobody documented: the sidecar quietly calling a metadata endpoint, the "deprecated" service still taking traffic, the pod hammering DNS. Because it's derived from syscalls, coverage is total by construction; there is no "we forgot to instrument that one."

The minimum requirement is a Linux kernel **≥ 5.1** (eBPF with BTF). Architecturally, the platform is the Coroot server (UI + inspections), node-agents, a cluster-agent, **Prometheus** for metrics, and **ClickHouse** for logs, traces, and profiles. Both stores are bundled but pluggable — you can point Coroot at the Prometheus you already run.

## Inspections: SLOs instead of a wall of alerts

The differentiating layer is what Coroot does with the data. Instead of shipping 400 alert rules, it ships **~80 predefined inspections** organized around SLOs: every application gets availability and latency objectives (defaults you can override per service), and inspections fire only in service of them. When an SLO burns, Coroot walks its model — is this OOM kills? CPU throttling? a Postgres dependency's latency? DNS failures? a deployment that landed ten minutes ago? — and presents the correlated evidence rather than twelve independent pages. It tracks Kubernetes deployments and compares each release against the previous one's baseline, which catches the "p99 doubled after Tuesday's rollout" class of regressions automatically. The mechanics of SLO burn alerting are the same as in the [burn-rate article](/articles/observability/2026-07-27-slo-burn-rate-alerts); Coroot just pre-wires them.

## Install and first look

On Kubernetes, the operator route is the recommended one:

```bash
helm repo add coroot https://coroot.github.io/helm-charts
helm repo update coroot

# Operator, then the Community Edition
helm install -n coroot --create-namespace coroot-operator coroot/coroot-operator
helm install -n coroot coroot coroot/coroot-ce

kubectl port-forward -n coroot service/coroot-coroot 8080:8080
```

For a single Docker host (say, the box running your MQTT broker and ingestion services):

```bash
curl -fsS https://raw.githubusercontent.com/coroot/coroot/main/deploy/docker-compose.yaml \
  | docker compose -f - up -d
```

That compose file starts five containers — coroot, node-agent, cluster-agent, Prometheus, and ClickHouse. Open `http://localhost:8080`, and within a couple of minutes the map populates with everything the node is actually doing. The node-agent needs privileged access (it's loading eBPF programs), which locked-down Pod Security Standards will make you say out loud.

## What it cannot see — and how it differs from Beyla/OBI

Be clear-eyed about the ceiling of the syscall vantage point:

- **No intra-service visibility.** eBPF sees requests enter and leave a process. It cannot tell you which function was slow, what the business context was, or attach a `device_id` attribute. That's what code-level OTel instrumentation is for.
- **Trace semantics are weaker.** Coroot captures and correlates spans from network activity (and ingests OTel traces if you have them), but eBPF-derived traces lack the deliberate, attribute-rich spans a developer writes. Context propagation across async hops (queues, MQTT) is largely invisible.
- **Encryption is a partial blind spot.** The agent hooks common TLS libraries (OpenSSL, Go's crypto/tls) to see plaintext at the library boundary, but service-mesh mTLS terminated in a sidecar, or exotic runtimes, can reduce protocol parsing to opaque TCP byte counts.
- **Linux only**, kernel 5.1+; no browser or mobile RUM.

Versus **Beyla/OBI**: Beyla is a *component* — an agent that emits OTLP into whatever backend you already run (Tempo, ClickStack, anything). Coroot is a *product* — agents plus storage plus opinionated UI and root-cause logic. Beyla fits when you have a pipeline and want eBPF as one more source; Coroot fits when you have nothing and want answers this afternoon. They're also complementary with real instrumentation: run Coroot for the always-on, everything-covered base layer, and add OTel SDKs to the services where you need business-level attributes — the same layering argument as the Beyla piece, one level up the stack.

**Try next:** Run the docker-compose one-liner on a host with a few services and open the service map — then find the one dependency edge you didn't know existed (there is always one) and decide whether it should.
