---
title: "OCB: Compiling a Minimal OpenTelemetry Collector Distribution"
date: 2026-08-15
track: observability
summary: "The contrib Collector compiles in hundreds of components that a given deployment never enables — each one binary weight, configuration surface, and CVE triage load. The OpenTelemetry Collector Builder (ocb) generates and compiles a distribution from a short manifest of pinned components. This article covers a complete builder-config.yaml, the build steps, per-component stability levels, and how the official k8s distribution is assembled from the same format."
reading_time: 6
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

**Gist.** The OpenTelemetry Collector is a static Go binary whose component set is fixed at compile time, so the widely deployed `otelcol-contrib` distribution carries the code for hundreds of receivers, processors, exporters and connectors regardless of how few a given pipeline enables. The **OpenTelemetry Collector Builder (`ocb`)** inverts this: a manifest lists the components required, and `ocb` generates a Go module registering exactly those and compiles it. The cost is an owned build pipeline — a Go toolchain, a version bump on every Collector release, and responsibility for the component selection that a vendor distribution would otherwise make.

## The property that makes distribution choice load-bearing

The Collector has no plugin loader. A component is available to configuration if and only if it was **registered in the binary at build time**; the YAML configuration selects among registered components rather than loading new ones. Three consequences follow, in the order they typically surface.

1. **Configuration surface equals compiled surface.** Every registered component is reachable from YAML. A binary that contains only the OpenTelemetry Protocol (OTLP) receiver, the batch processor and one exporter **cannot** be configured to open a Zipkin port or scrape a cloud metadata endpoint, because that code is absent. Restricting the binary is therefore a stronger control than restricting the configuration file, which can be edited.
2. **Vulnerability triage covers the whole module graph.** Contrib's `go.mod` pulls the transitive dependencies of every bundled component. A scanner reports findings against code that is present in the binary irrespective of whether the enclosing component is enabled, so patch releases are driven by components the deployment never instantiates.
3. **Image size and process footprint.** Fewer compiled components mean a smaller image to pull, which is amplified when the Collector runs as a DaemonSet with one pod per node.

The independent guides converge on the same split: contrib for evaluation, a curated or purpose-built distribution for production.

## The manifest

`ocb` consumes a single YAML manifest with a `dist` block and one list per component kind. Versions follow the Collector's release train: core and contrib components share the `v0.x` series — **`v0.158.0`** at the time of writing — while the stable `confmap` providers are versioned on the separate **`v1.x`** line. Both lines are cut in the same release, and the `v1.x` version that pairs with a given `v0.x` release is the one listed in that release's notes — it is not derivable from the `v0.x` number. A manifest for a trace-and-metrics gateway:

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

The two module prefixes matter. Core components live under `go.opentelemetry.io/collector/...`; contrib components live under `github.com/open-telemetry/opentelemetry-collector-contrib/...`. **The manifest is the point at which individual contrib components are taken without taking the contrib distribution**, one line per component — the transform processor (the OpenTelemetry Transformation Language, OTTL) and the span-metrics connector enter this way.

The `providers` list is easy to under-populate. Confmap providers implement the URI schemes the configuration loader understands; omitting `envprovider` removes the ability to resolve `${env:...}` references in the configuration, and omitting `fileprovider` removes `file:` resolution. **A missing provider surfaces as a configuration-resolution failure at startup, not as a build error**, because the manifest and the runtime configuration are checked at different times.

## Build

The `ocb` binary is published on the `opentelemetry-collector-releases` releases page alongside the official distributions.

```bash
curl -sLO https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v0.158.0/ocb_0.158.0_linux_amd64
chmod +x ocb_0.158.0_linux_amd64 && mv ocb_0.158.0_linux_amd64 ocb

./ocb --config builder-config.yaml
# generates Go sources + compiles: ./otelcol-acme/otelcol-acme

./otelcol-acme/otelcol-acme --config collector-config.yaml
```

The build is two phases. `ocb` first **generates an ordinary Go module** — `main.go`, `components.go` and `go.mod` — in `output_path`, where `components.go` is the generated registry mapping each component's type name to its factory; it then invokes `go build`. This requires **a Go toolchain on the build machine** at the version the Collector's own modules require, which in container workflows means a multi-stage Dockerfile whose first stage holds the toolchain and whose final stage carries only the resulting binary into a distroless image.

Because the intermediate artefact is plain Go source over a dependency set fixed by the manifest, the generated module can be vendored, scanned with `govulncheck`, and attested like any other first-party Go build. Tracking upstream is then a version edit across the manifest's `gomod` lines per Collector release, which is the part worth automating in continuous integration (CI).

## Stability is per component and per signal

Hand-picking components forces an explicit encounter with their maturity, which varies widely inside a single distribution. The Collector's `component-stability.md` defines six levels — **development**, **alpha**, **beta**, **stable**, plus **deprecated** and **unmaintained** — and applies them **per signal**, so one component can be stable for metrics while its traces support is beta. Two of the levels carry concrete operational consequences: **an alpha component's configuration may change in breaking ways between releases**, and **a component left without an active code owner is marked unmaintained and may eventually be removed**. Contrib ships components at all of these levels side by side, so its inclusion of a component implies nothing about that component's stability. A curated manifest moves the reading of each component's stability badge to authoring time rather than to the upgrade that breaks.

## The official distributions use the same format

This is the project's own build path, not a side workflow. The `opentelemetry-collector-releases` repository holds a `distributions/` directory in which each official distribution — `otelcol` core, `otelcol-contrib`, `otelcol-k8s`, the minimal `otelcol-otlp`, and the eBPF profiler distribution — is defined by a `manifest.yaml` in exactly the format above and built with `ocb` in CI.

The **k8s distribution** is the informative example of curation against a deployment shape. It pins the receivers a cluster deployment reads from — `k8s_cluster`, `k8sobjects`, `kubeletstats`, `hostmetrics`, `filelog` and OTLP — alongside the Kubernetes-aware processors `k8sattributes` and `resourcedetection`, and stops there: a component count in the tens rather than the hundreds, and no entry for the many contrib components that have nothing to do with Kubernetes. Copying that manifest and deleting the unused entries is a supported starting point. Vendor distributions such as Grafana Alloy occupy the other end of the same trade-off: the same upstream components under someone else's curation, with no build pipeline to own.

A direct measurement of the difference is available without instrumentation: compare `ls -lh` on `otelcol-contrib` against the `ocb` output for an identical pipeline configuration, then run `govulncheck ./...` against the generated module. The delta in binary size and in reachable findings is the case for or against the custom build.

## Pitfalls

- **A component enabled in the Collector configuration but absent from the manifest fails at startup**, not at build time, with an unknown-type error — the manifest and the runtime configuration are separate files with no cross-check.
- **Omitting a confmap provider silently removes a URI scheme.** Without `envprovider`, `${env:...}` references do not resolve and the Collector refuses to start, even though the build succeeded.
- **Mixing version lines produces module resolution failures.** Core and contrib components use the `v0.x` series while stable confmap providers use `v1.x`; pinning a provider to a `v0.x` version, or leaving one component behind on an upgrade, breaks the build rather than degrading at runtime.
- **Contrib membership is not a stability signal.** A component may be alpha for the signal in use, and alpha configuration may change in breaking ways between releases, so an upgrade that only bumps versions can still invalidate the configuration file.
- **A component left without an active code owner can be marked unmaintained and later removed**, so a manifest pinned to an old release can fail to build against a newer one because the module path no longer exists.
- **The build machine needs the Go toolchain, not only the runtime.** A CI image that carries only the collector binary cannot run `ocb`, which shells out to `go build` after generating sources.
