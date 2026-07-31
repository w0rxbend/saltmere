---
title: "The servicegraph connector: metrics for the edges between services"
date: 2026-07-31
track: observability
summary: "spanmetrics tells you how each service is doing; the servicegraph connector tells you about the calls between them. Here is how it pairs client and server spans into per-edge request, error, and latency metrics."
reading_time: 5
tags: [opentelemetry, servicegraph, service-map, tempo, traces, topology]
sources:
  - title: "Service Graph Connector README - opentelemetry-collector-contrib"
    url: "https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/connector/servicegraphconnector/README.md"
  - title: "Service graphs - Grafana Tempo documentation"
    url: "https://grafana.com/docs/tempo/latest/metrics-from-traces/service_graphs/"
  - title: "otelcol.connector.servicegraph - Grafana Alloy documentation"
    url: "https://grafana.com/docs/alloy/latest/reference/components/otelcol/otelcol.connector.servicegraph/"
  - title: "How to Generate Service Graph Metrics from Traces in the Collector - OneUptime"
    url: "https://oneuptime.com/blog/post/2026-02-06-generate-service-graph-metrics-traces-collector/view"
---

Most RED-metrics tooling answers "how is service X doing?" The servicegraph connector answers a different question: "how is the *call from* X *to* Y doing?" It turns raw spans into metrics keyed by the **edge** between two services, which is exactly what a node graph needs to draw arrows with volume and error rates on them. It is the same code that powers Grafana Tempo's service graphs, packaged as an OpenTelemetry Collector connector.

## How it pairs spans

A single logical request usually produces two spans on the same trace: a `client` (or `SPAN_KIND_CLIENT`) span on the caller and a `server` span on the callee. Neither span alone tells you there is an edge; you only learn the edge exists when you see both and confirm they belong to the same request.

The connector does this by keeping every "pairable" span in an in-memory store, keyed so that its partner can find it. When the matching span arrives, it emits one edge data point and evicts the pair. It recognises three request shapes:

- **Direct requests** — a `client` span paired with the corresponding `server` span.
- **Messaging** — a `producer` span paired with a `consumer` span.
- **Database** — a `client` span carrying database attributes (e.g. `db.system`), where the database becomes a virtual node.

If a partner never arrives before `store.ttl`, the span is dropped and counted as unpaired. That TTL is the whole ballgame: it must comfortably exceed your longest realistic hop latency, or slow calls silently vanish from the graph. The default is a conservative `2s`.

## The metrics it produces

Every metric is labelled with `client`, `server`, and `connection_type` (unset, `virtual_node`, `messaging_system`, or `database`). That `client`/`server` pair *is* the edge.

- `traces_service_graph_request_total` — counter of completed request pairs per edge.
- `traces_service_graph_request_failed_total` — counter of pairs where either span failed. Divide it by the total for a per-edge error rate.
- `traces_service_graph_request_server_seconds` — histogram of latency measured on the server span.
- `traces_service_graph_request_client_seconds` — histogram of latency measured on the client span. The gap between client and server latency is network + queueing time.
- `traces_service_graph_unpaired_spans_total` and `traces_service_graph_dropped_spans_total` — health of the pairing itself. If these climb, your TTL is too short or one side is uninstrumented.

(In the OTel Collector README the histograms are documented as `..._request_server` / `..._request_client`; Prometheus/Tempo expose them with the `_seconds` suffix and the usual `_bucket`/`_sum`/`_count` families.)

## Store and dimensions

Two knobs dominate. `store.ttl` bounds how long a lonely span waits for its partner; `store.max_items` (default `1000`) caps memory. Cardinality is the trap here: an edge exists for every `client`x`server` pair, and every extra `dimensions` entry multiplies that. For N services you can approach N-squared edges before dimensions even enter the picture, so add dimensions like `http.method` deliberately, not reflexively. `virtual_node_peer_attributes` (default `[peer.service, db.name, db.system]`) controls how uninstrumented callees get named as virtual nodes instead of disappearing.

## Wiring it in the Collector

A connector is both an exporter (of the traces pipeline) and a receiver (of a metrics pipeline). You export spans *into* the connector and receive metrics *out* of it:

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317

connectors:
  servicegraph:
    store:
      ttl: 5s            # >= ~2x your longest hop latency
      max_items: 10000
    latency_histogram_buckets: [10ms, 50ms, 100ms, 250ms, 500ms, 1s, 2.5s, 5s, 10s]
    dimensions:
      - http.method

exporters:
  prometheus:
    endpoint: 0.0.0.0:8889

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [servicegraph]      # spans flow INTO the connector
    metrics/servicegraph:
      receivers: [servicegraph]      # edge metrics flow OUT
      exporters: [prometheus]
```

Point Grafana's node-graph panel at those metrics: `sum by (client, server) (rate(traces_service_graph_request_total[5m]))` sizes each arrow by call volume, and the ratio of `_failed_total` to total colours it by error rate.

## Why this is not spanmetrics

The spanmetrics connector (covered in its own article here) aggregates spans **per service** into RED metrics — one node's rate, errors, and duration. It knows nothing about who called whom. The servicegraph connector aggregates **per edge** by joining two spans across the trace. spanmetrics colours the *nodes*; servicegraph draws and colours the *arrows* between them. They are complementary, run happily as two connectors on the same traces pipeline, and answer different questions: "which service is unhealthy?" versus "which *dependency* is dragging it down?"

**Try next:** Run servicegraph and spanmetrics side by side on the same traces pipeline, then artificially throttle one downstream service and watch which signal moves first — the callee's spanmetrics error rate, or the caller-to-callee edge's `traces_service_graph_request_failed_total`. Then shrink `store.ttl` below that service's latency and confirm the edge drops out while `unpaired_spans_total` climbs.
