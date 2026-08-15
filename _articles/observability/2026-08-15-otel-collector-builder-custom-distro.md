---
title: "OCB: Build the OpenTelemetry Collector You Actually Need"
date: 2026-08-15
track: observability
summary: "The contrib Collector ships hundreds of components you will never enable — each one attack surface, binary weight, and CVE exposure. The OpenTelemetry Collector Builder (ocb) compiles a distribution from a ~20-line manifest of pinned components. Here's a complete builder-config.yaml, the build commands, and how the official k8s distro is assembled the same way."
reading_time: 5
tags: [opentelemetry, collector, ocb, otel-collector-builder, distributions, supply-chain]
sources:
  - title: "OpenTelemetry docs — Build a custom collector with OCB"
    url: "https://opentelemetry.io/docs/collector/extend/ocb/"
  - title: "opentelemetry-collector-releases — GitHub releases (ocb binaries + official distros)"
    url: "https://github.com/open-telemetry/opentelemetry-collector-releases/releases"
  - title: "otelcol-k8s distribution manifest.yaml"
    url: "https://github.com/open-telemetry/opentelemetry-collector-releases/blob/main/distributions/otelcol-k8s/manifest.yaml"
  - title: "opentelemetry-collector — component-stability.md"
    url: "https://github.com/open-telemetry/opentelemetry-collector/blob/main/docs/component-stability.md"
  - title: "Dash0 — Building a Custom OpenTelemetry Collector (independent guide)"
    url: "https://www.dash0.com/guides/custom-opentelemetry-collector"
---

Most teams run `otelcol-contrib` in production because it's the distribution that has everything. That's exactly the problem. Contrib bundles hundreds of components — every receiver from Kafka to vCenter, exporters for a dozen vendors you don't use — and each one is compiled-in Go code: extra binary size, slower CVE triage (a vulnerability in *any* vendored component pages *you*), and configuration surface an attacker or a well-meaning teammate can enable. The project's own answer is the **OpenTelemetry Collector Builder (`ocb`)**: declare the handful of components you actually use in a manifest, compile a distribution containing only those. As of **v0.158.0** (August 2026), `ocb` ships as a binary alongside the official distros in the `opentelemetry-collector-releases` repo.

## Why the fat distro is a liability

Three concrete reasons, in the order they usually bite:

1. **Attack surface.** Every registered component is reachable from YAML. A collector that physically contains only `otlp`, `batch`, and your one exporter cannot be misconfigured into scraping cloud metadata or opening a Zipkin port.
2. **Vulnerability management.** Contrib's go.mod pulls in hundreds of transitive dependencies. Your scanner will flag CVEs in components you never enable, and you'll ship patch releases for them anyway because the binary contains the code.
3. **Size and startup.** Fewer components means a smaller image to pull on every node — this matters at DaemonSet scale — and a faster, smaller process.

The independent guides (Dash0's is a good one) all converge on the same recommendation: contrib is for evaluation; production wants a custom or purpose-built distro.

## The manifest: pin everything

`ocb` consumes a single YAML manifest. Component versions follow the Collector's release train — core and contrib components at `v0.158.0`, stable confmap providers on the `v1.x` line (`v1.48.0` pairs with 0.158.0). A complete, working `builder-config.yaml` for a typical trace/metrics gateway:

```yaml
dist:
  name: otelcol-acme
  description: Acme's minimal production collector
  output_path: ./otelcol-acme
  version: 1.0.0

receivers:
  - gomod: go.opentelemetry.io/collector/receiver/otlpreceiver v0.158.0

processors:
  - gomod: go.opentelemetry.io/collector/processor/batchprocessor v0.158.0
  - gomod: go.opentelemetry.io/collector/processor/memorylimiterprocessor v0.158.0
  - gomod: github.com/open-telemetry/opentelemetry-collector-contrib/processor/transformprocessor v0.158.0

connectors:
  - gomod: github.com/open-telemetry/opentelemetry-collector-contrib/connector/spanmetricsconnector v0.158.0

exporters:
  - gomod: go.opentelemetry.io/collector/exporter/otlpexporter v0.158.0
  - gomod: go.opentelemetry.io/collector/exporter/debugexporter v0.158.0

providers:
  - gomod: go.opentelemetry.io/collector/confmap/provider/envprovider v1.48.0
  - gomod: go.opentelemetry.io/collector/confmap/provider/fileprovider v1.48.0
```

Note the mix: core components come from `go.opentelemetry.io/collector/...`, contrib ones from the `opentelemetry-collector-contrib` module — the manifest is where you cherry-pick contrib without swallowing it whole. The `transformprocessor` and `spanmetricsconnector` above are the OTTL and span-metrics pieces we've covered before; they slot in as one line each.

## Build and run

Grab `ocb` for your platform from the collector-releases page and build:

```bash
curl -sLO https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v0.158.0/ocb_0.158.0_linux_amd64
chmod +x ocb_0.158.0_linux_amd64 && mv ocb_0.158.0_linux_amd64 ocb

./ocb --config builder-config.yaml
# generates Go sources + compiles: ./otelcol-acme/otelcol-acme

./otelcol-acme/otelcol-acme --config collector-config.yaml
```

`ocb` generates a real Go module (`main.go`, `components.go`, `go.mod`) and runs `go build` — you need Go ≥ 1.24 on the build machine, or run it in a multi-stage Dockerfile and copy the static binary into a distroless image. Because the output is ordinary Go source, you can also vendor it, run `govulncheck` against it, and get supply-chain attestation over a dependency set you chose, not one chosen for you. Rebuilding on each Collector release is a one-line version bump — worth automating in CI.

## Stability is per-component, per-signal

The other reason to hand-pick: components mature at very different rates. The Collector defines six stability levels — **development**, **alpha**, **beta**, **stable**, plus **deprecated** and **unmaintained** — and they're assessed *per signal*: a receiver can be stable for metrics while its traces support is still beta. Alpha components may change configuration "with minimal notice"; unmaintained ones can be removed after three months without a code owner. Contrib happily ships all of these side by side. A curated manifest forces you to look up each component's badge in its README once, instead of discovering an alpha config break during an upgrade.

## Official distros are just manifests too

This isn't a niche workflow — it's how the project builds its own artifacts. The `opentelemetry-collector-releases` repo contains a `distributions/` directory where each official distro (`otelcol` core, `otelcol-contrib`, `otelcol-k8s`, the minimal `otelcol-otlp`, the eBPF profiler distro) is defined by exactly the same `manifest.yaml` format and built with `ocb` in CI. The **k8s distribution** is the instructive one: instead of contrib's everything, it pins a Kubernetes-shaped subset — `k8s_cluster`, `k8sobjects`, `kubeletstats`, `hostmetrics` and OTLP receivers; `k8sattributes`, `resourcedetection`, tail-sampling and transform processors; the `spanmetrics` and `servicegraph` connectors; the `opamp` extension for fleet management — a few dozen components instead of several hundred. Copying that manifest and deleting what you don't need is a legitimate way to start. (If you'd rather not own a build pipeline at all, that's essentially the pitch of vendor distros like Grafana Alloy — same components, someone else's curation.)

**Try next:** run `go tool nm` or just `ls -lh` on `otelcol-contrib` versus your `ocb` output for the same pipeline config, then point `govulncheck ./...` at the generated module — the delta in binary size and reachable CVEs is the business case in two numbers.
