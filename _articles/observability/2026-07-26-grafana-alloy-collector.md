---
title: "Grafana Alloy: the OTel Collector distribution that replaced Grafana Agent"
date: 2026-07-26
track: observability
summary: "Grafana Agent reached end-of-life on November 1, 2025. Its successor, Grafana Alloy, is an OpenTelemetry Collector distribution whose pipelines are expressed as a graph of referenced components rather than an ordered YAML list."
reading_time: 6
tags: [grafana-alloy, opentelemetry, collector, observability, grafana-agent, prometheus]
sources:
  - title: "Grafana Alloy documentation"
    url: "https://grafana.com/docs/alloy/latest/"
  - title: "From Agent to Alloy: OpenTelemetry Collector with Prometheus pipelines (Grafana Labs)"
    url: "https://grafana.com/blog/grafana-alloy-opentelemetry-collector-with-prometheus-pipelines/"
  - title: "OpenTelemetry in Alloy — the OTel Engine"
    url: "https://grafana.com/docs/alloy/latest/introduction/otel_alloy/"
  - title: "Grafana Agent documentation (deprecation / migrate to Alloy)"
    url: "https://grafana.com/docs/agent/latest/"
  - title: "grafana/alloy releases (GitHub)"
    url: "https://github.com/grafana/alloy/releases"
---

**Gist.** Grafana Agent, the long-standing collection agent for Prometheus metrics and Grafana-stack logs, reached **End-of-Life on November 1, 2025**, leaving deployed binaries without security or bug fixes. Its replacement, **Grafana Alloy**, is a distribution of the upstream OpenTelemetry (OTel) Collector that embeds Grafana's Prometheus-native scraping in the same binary, so one process serves both OpenTelemetry Protocol (OTLP) push traffic and Prometheus pull traffic. The cost of that consolidation is a different configuration model: pipelines are no longer ordered lists in YAML but a **dependency graph the operator wires by reference**, which changes how misconfiguration presents itself.

This article treats Alloy strictly as the collection agent — what it is, how the component syntax composes a pipeline, and where that composition fails. Tracing instrumentation and eBPF-based profiling are separate subjects.

## From Agent to Alloy: the timeline

Grafana Agent shipped in three forms: Static mode, Flow mode, and the Agent Operator. Alloy is the continuation of the Flow-mode component model; Static mode and the Operator were deprecated rather than carried forward.

| Milestone | Date |
|---|---|
| Grafana Alloy announced; Agent deprecated, enters Long-Term Support | April 9, 2024 |
| Grafana Agent LTS ends | October 31, 2025 |
| Grafana Agent **End-of-Life** (no security/bug fixes) | November 1, 2025 |

Alloy releases on its own 1.x line; the current version is published on the
`grafana/alloy` releases page rather than fixed by this article.

Alloy is not a rename. It embeds the upstream **OpenTelemetry Collector as its "OTel Engine"**, so each `otelcol.*` component corresponds to a real upstream receiver, processor, or exporter rather than a reimplementation of one. Alongside that engine, Alloy retains Grafana's own component runtime and the Prometheus collection components (`prometheus.scrape`, `prometheus.remote_write`, the `discovery.*` family). **Upstream OTel compatibility and first-class Prometheus scraping therefore coexist in a single binary**, which is the property that lets Alloy replace several agents at once.

## The component pipeline model

Alloy's configuration language — an HCL-like syntax formerly named River — describes the pipeline as a graph of **components**. A component declaration carries three things: a **type** (`prometheus.scrape`), a **label** (`"node"`), and a block of arguments. Components also publish **exports**: values other components may reference. For OTel components the export is `.input`; for Prometheus write paths it is `.receiver`.

The edge in the graph is the reference itself. Writing `forward_to = [prometheus.remote_write.default.receiver]` inside `prometheus.scrape "node"` both names the destination and creates the dependency. **There is no ordered pipeline list and no `service:` block declaring order**, as there is in the upstream Collector's YAML; Alloy derives execution order from the references and evaluates the resulting graph.

The topology remains the familiar **receivers → processors → exporters**, but each stage names the entry point of the next rather than appearing at a fixed position in a list. Two consequences follow directly from the model:

- **A component with no inbound reference is still evaluated but receives no data.** Nothing in the syntax marks a stage as unreachable, because reachability is a property of the reference graph rather than of any declaration.
- **Fan-out and fan-in are expressed by list membership.** `forward_to` and the `output` block take lists, so one receiver may feed several processors and several receivers may feed one exporter without additional syntax.

## A runnable configuration

The following file scrapes a Prometheus target and remote-writes it, and independently receives OTLP and exports it over OTLP/HTTP. It is saved as `config.alloy`.

```alloy
// --- Prometheus path: scrape a local node_exporter and remote_write it ---
prometheus.scrape "node" {
  targets    = [{ "__address__" = "localhost:9100" }]
  forward_to = [prometheus.remote_write.default.receiver]
  scrape_interval = "15s"
}

prometheus.remote_write "default" {
  endpoint {
    url = "https://prometheus.example.com/api/v1/write"
  }
}

// --- OTel path: receive OTLP, batch, export over OTLP/HTTP ---
otelcol.receiver.otlp "in" {
  grpc { endpoint = "0.0.0.0:4317" }
  http { endpoint = "0.0.0.0:4318" }

  output {
    metrics = [otelcol.processor.batch.default.input]
    traces  = [otelcol.processor.batch.default.input]
    logs    = [otelcol.processor.batch.default.input]
  }
}

otelcol.processor.batch "default" {
  output {
    metrics = [otelcol.exporter.otlphttp.out.input]
    traces  = [otelcol.exporter.otlphttp.out.input]
    logs    = [otelcol.exporter.otlphttp.out.input]
  }
}

otelcol.exporter.otlphttp "out" {
  client {
    endpoint = "https://otlp.example.com"
  }
}
```

Two properties of this file are load-bearing. First, the **`output` block is per-signal**: `metrics`, `traces`, and `logs` are independent lists, so omitting one silently drops that signal at the stage where it was omitted rather than raising an error. Second, the **two paths never meet** — the Prometheus scrape does not traverse `otelcol.processor.batch`, because no reference connects them. Sharing a processor between the paths requires an explicit bridging component, not proximity in the file.

The process is started with:

```bash
alloy run config.alloy
```

Alloy then serves a built-in debugging user interface at `http://localhost:12345`. Its graph view renders the same component references declared above, and each node exposes the component's live state and health, so an unreferenced or unhealthy component is visible as a graph property rather than inferred from log lines.

Three operational controls are documented for this runtime:

- **`--stability.level`** gates components by stability level; a component whose own stability is below the configured level refuses to start, so components still in public preview or experimental require the flag to be lowered from the default generally-available setting.
- **Live reload:** sending `SIGHUP`, or calling the reload endpoint, applies configuration changes without restarting the process.
- **`alloy convert`** translates existing Grafana Agent (Static and Flow) and Prometheus configurations into Alloy syntax, which is the documented migration path off the end-of-life agent.

## What the consolidation buys

Because Alloy is an OTel Collector distribution rather than an agent that speaks OTLP, an application fleet already emitting OTLP can point its SDKs directly at `otelcol.receiver.otlp`. A fleet still exposing Prometheus endpoints continues to be scraped by `prometheus.scrape` and shipped by `prometheus.remote_write`. Both run in one process and can share processors — batching, filtering, relabelling — expressed as components referenced by name instead of YAML stanzas ordered by hand.

The extension of the configuration above is a `discovery.kubernetes` component whose exported target list feeds `prometheus.scrape`, replacing the static `targets` literal with pods discovered at runtime; `alloy convert --source-format=prometheus` performs the equivalent translation for an existing `prometheus.yml`.

## Pitfalls

- **A `grafana-agent` binary left running after November 1, 2025 receives no patches.** End-of-life means the upstream project ships neither security nor bug fixes, so the exposure grows with time rather than staying constant.
- **Omitting a signal from an `output` block drops that signal silently.** The block is per-signal; a `batch` processor that lists only `metrics` forwards no traces or logs, and the configuration is still valid.
- **A component declared but never referenced collects nothing.** Reachability comes from the reference graph, not from declaration order, so a correctly configured exporter that no processor names produces an idle, healthy-looking component.
- **Proximity in the file implies nothing.** Prometheus-path and OTel-path components adjacent in the config remain disconnected unless a reference joins them.
- **Migrating Static-mode Agent configuration by hand invites omissions.** `alloy convert` exists precisely because Static mode has no component structure to translate mechanically by eye.
- **Experimental components fail to start under the default stability level.** The symptom is a startup error naming the component, not a runtime data gap, and the cause is that `--stability.level` still sits at its default and does not admit that component's stability.
