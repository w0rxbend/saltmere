---
title: "Grafana Alloy: the OTel Collector that replaced Grafana Agent"
date: 2026-07-26
track: observability
summary: "Grafana Agent hit end-of-life on November 1, 2025. Its successor, Grafana Alloy, is an OpenTelemetry Collector distribution with a component-based pipeline syntax. Here's what changed and a working config you can run today."
reading_time: 5
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

If you still have `grafana-agent` binaries running in production, they are now unsupported software. Grafana Agent reached **End-of-Life on November 1, 2025** — no more security patches, no more bug fixes. Its replacement is **Grafana Alloy**, a vendor-neutral OpenTelemetry Collector distribution that collects metrics, logs, traces, and profiles through programmable pipelines.

This post is about Alloy as the *collection agent*: what it is, why Agent went away, and how its component syntax wires a pipeline together. If you want the tracing or eBPF side, those live in separate posts — here we stay on the collector.

## From Agent to Alloy: the timeline

Grafana Agent had three flavors — Static mode, Flow mode, and the Operator. Rather than keep maintaining a bespoke agent alongside the industry's convergence on OpenTelemetry, Grafana folded the good parts of Flow mode into a new project and deprecated the rest.

| Milestone | Date |
|---|---|
| Grafana Alloy announced; Agent deprecated, enters Long-Term Support | April 9, 2024 |
| Grafana Agent LTS ends | October 31, 2025 |
| Grafana Agent **End-of-Life** (no security/bug fixes) | November 1, 2025 |
| Current Alloy release line | v1.17.x (mid-2026) |

Alloy is not a rename — it is an OTel Collector distribution. Under the hood it embeds the upstream **OpenTelemetry Collector** as its "OTel Engine," so every `otelcol.*` component maps to a real upstream receiver, processor, or exporter. On top of that, Alloy keeps Grafana's Prometheus-native collection and its own component runtime — you get upstream OTel compatibility *plus* first-class Prometheus scraping in one binary.

## The component pipeline model

Alloy's config language (formerly called River, an HCL-ish syntax) describes a pipeline as a graph of **components**. Each component has a type, a label, and arguments, and you wire them together by referencing another component's exported value — usually a `receiver` or an `.input`. There are no ordered pipeline lists like the OTel Collector's YAML; you connect components explicitly by reference, and Alloy resolves the dependency graph.

The shape is always: **receivers → processors → exporters**, where each stage names the next one's entry point.

## A config you can run

Here is a complete Alloy file that scrapes a Prometheus target and remote-writes it, plus receives OTLP and forwards it to an OTLP endpoint. Save it as `config.alloy`.

```alloy
// --- Prometheus path: scrape a local node_exporter and remote_write it ---
prometheus.scrape "node" {
  targets    = [{ __address__ = "localhost:9100" }]
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

Notice the wiring: `prometheus.scrape.node` forwards to `prometheus.remote_write.default.receiver`, and each OTel stage names the next component's `.input`. That reference *is* the pipeline — there is no separate `service:` block declaring order.

Run it:

```bash
alloy run config.alloy
```

Alloy starts, serves a built-in debugging UI at `http://localhost:12345`, and begins collecting. The UI's graph view renders exactly the component references above, so you can watch data flow and inspect each component's live state and health.

A few things worth knowing:

- **`--stability.level`** gates experimental components; stable ones run by default.
- **Live reload:** send `SIGHUP` (or hit the reload endpoint) to apply config changes without a restart.
- **Migration help:** `alloy convert` translates existing Grafana Agent (Flow/Static) and Prometheus configs into Alloy syntax to get you off the EOL'd agent quickly.

## Why this matters

The practical takeaway is that Alloy lets one agent replace a pile of them. Because it is a real OTel Collector distribution, a team already invested in OTLP can point SDKs straight at Alloy's `otelcol.receiver.otlp`. A team still living in Prometheus can keep scraping with `prometheus.scrape` and `remote_write`. And both can run in the same binary, sharing processors like batching, filtering, or relabeling — expressed as components you reference rather than YAML stanzas you order by hand.

**Try next:** Add a `discovery.kubernetes` component and feed its exported targets into `prometheus.scrape` so Alloy auto-discovers pods to scrape, then run `alloy convert --source-format=prometheus` on an existing `prometheus.yml` to see how your current scrape jobs translate into Alloy components.
