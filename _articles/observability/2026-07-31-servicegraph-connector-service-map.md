---
title: "The servicegraph connector: metrics for the edges between services"
date: 2026-07-31
track: observability
summary: "spanmetrics aggregates spans per service; the servicegraph connector aggregates them per call between services. How client and server spans are paired in a TTL-bounded store into per-edge request, error, and latency metrics."
reading_time: 6
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

**Gist.** Rate-errors-duration (RED) metrics derived from spans describe individual services, but a topology view needs metrics attached to the *call* from one service to another, and no single span carries that fact. The servicegraph connector recovers it by holding each pairable span in an in-memory store until its counterpart arrives on the same trace, then emitting one data point labelled by the `client`/`server` edge. The cost is a bounded, lossy join: a span whose partner does not arrive within `store.ttl`, or that is evicted when the store reaches `store.max_items`, is dropped and its edge never observed.

## The pairing problem

A single logical remote call normally produces two spans on the same trace: a `client` span (`SPAN_KIND_CLIENT`) recorded by the caller and a `server` span recorded by the callee. Neither span in isolation establishes an edge. The caller's span names the operation it invoked but not necessarily the identity of the service that handled it; the callee's span names itself but not who called it. **The edge is a join, not an attribute** — it exists only once both halves have been observed and confirmed to belong to the same request.

The connector implements that join as a streaming, TTL-bounded hash join. Every span that could participate in a pair is inserted into an in-memory store keyed such that its counterpart, arriving later and possibly from a different Collector receiver, hashes to the same entry. On a hit the connector emits one edge data point and evicts the pair; the store therefore holds only *half-pairs*, and its steady-state size is proportional to the number of calls currently in flight rather than to total throughput.

Three request shapes are recognised:

- **Direct requests** — a `client` span paired with the corresponding `server` span.
- **Messaging** — a `producer` span paired with a `consumer` span.
- **Database** — a `client` span carrying database attributes (for example `db.system`), where the database itself becomes a virtual node with no span of its own.

## The invariant and the failure mode

The store enforces two bounds, and each one is a way to lose an edge.

`store.ttl` bounds how long a lonely span waits. **If the partner has not arrived when the TTL expires, the span is evicted and counted as unpaired, and the request contributes to no edge at all.** This is the sharp failure mode: the TTL is compared against the wall-clock gap between the two spans reaching the connector, which includes the actual hop latency plus batching delay in each exporter and any queueing on the way in. A TTL shorter than a service's real tail latency does not degrade that edge's numbers — it removes the slow requests from the edge entirely, which biases the latency histogram *downwards* precisely for the dependency that is misbehaving. The default is `2s`.

`store.max_items` bounds memory, with a default of `1000` half-pairs. When the store is full, an insertion costs a span: one half-pair is dropped and its edge is never emitted. Under a burst of concurrent in-flight calls this produces loss that is independent of the TTL.

Both losses are observable, which is what makes the connector operable at all: `traces_service_graph_unpaired_spans_total` and `traces_service_graph_dropped_spans_total` are the health signal for the join itself. **A rising unpaired count points at one of two causes: the TTL is shorter than the gap between the halves, or one side of the edge is not instrumented**; interpreting edge rates without watching these two counters means interpreting a join of unknown completeness.

## The emitted metrics

Every series is labelled with `client`, `server`, and `connection_type` (unset, `virtual_node`, `messaging_system`, or `database`). The `client`/`server` pair *is* the edge identity.

- `traces_service_graph_request_total` — counter of completed request pairs per edge.
- `traces_service_graph_request_failed_total` — counter of pairs in which either span failed. Its ratio to the total is the per-edge error rate.
- `traces_service_graph_request_server_seconds` — latency histogram measured on the server span.
- `traces_service_graph_request_client_seconds` — latency histogram measured on the client span. The difference between the two distributions covers network transit and queueing on both sides.
- `traces_service_graph_unpaired_spans_total`, `traces_service_graph_dropped_spans_total` — pairing health, as above.

Being histograms, the two latency series appear in Prometheus with the usual `_bucket`, `_sum` and `_count` families.

## Cardinality

An edge exists for each observed `client`x`server` combination, so for N services the edge count can approach N² before any further labels are considered. Each entry in `dimensions` promotes a span attribute to a label and multiplies that count by the attribute's distinct-value count, so `http.method` is a small constant factor while a route or identifier attribute is not.

`virtual_node_peer_attributes` (default `[peer.service, db.name, db.system]`) determines how an uninstrumented callee is named. Without a matching attribute the peer has no identity and the edge is lost rather than rendered as a virtual node.

### Implementation sketch (Scala)

The load-bearing idea is the TTL-bounded half-pair store; the eviction path, not the hit path, is where edges are lost.

```scala
enum Half { case Client, Server }

final case class Span(traceId: String, spanId: String, parentId: String,
                      service: String, half: Half, durationNanos: Long, failed: Boolean)

final case class Edge(client: String, server: String, failed: Boolean)

final class PairStore(ttl: Long, maxItems: Int):
  // A client span keys on its own (traceId, spanId); the server span it caused
  // is its child, so it keys on (traceId, parentId) and lands on the same entry.
  private val pending = scala.collection.mutable.LinkedHashMap.empty[(String, String), (Span, Long)]

  private def key(s: Span): (String, String) =
    s.half match
      case Half.Client => (s.traceId, s.spanId)
      case Half.Server => (s.traceId, s.parentId)

  def consume(s: Span, now: Long): Option[Edge] =
    expire(now)
    pending.remove(key(s)) match
      case Some((other, _)) if other.half != s.half =>
        val (c, srv) = if s.half == Half.Client then (s, other) else (other, s)
        Some(Edge(c.service, srv.service, c.failed || srv.failed))
      case _ =>
        if pending.size >= maxItems then pending.headOption.foreach((k, _) => pending.remove(k))
        pending.put(key(s), (s, now))
        None                                  // half-pair parked; may never complete

  private def expire(now: Long): Unit =
    pending.filterInPlace((_, v) => now - v._2 < ttl)   // dropped here counts as unpaired
```

## Wiring in the Collector

A connector acts as an exporter of one pipeline and a receiver of another. Spans are exported *into* it; metrics are received *out* of it.

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317

connectors:
  servicegraph:
    store:
      ttl: 5s
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

A Grafana node-graph panel sizes each arrow by `sum by (client, server) (rate(traces_service_graph_request_total[5m]))` and colours it by the ratio of `_failed_total` to the total.

## Relation to spanmetrics

The spanmetrics connector aggregates spans **per service** into RED metrics and carries no information about the caller. The servicegraph connector aggregates **per edge** by joining two spans across a trace. spanmetrics colours the nodes; servicegraph draws and colours the arrows. Both can run as connectors on the same traces pipeline, answering "which service is unhealthy?" and "which dependency is dragging it down?" respectively.

## Pitfalls

- Setting `store.ttl` below a dependency's tail latency does not merely delay those edges: the slow requests are evicted as unpaired, so the edge's latency histogram under-reports exactly when that dependency is slow.
- The TTL is measured against arrival at the connector, not against span timestamps, so exporter batching intervals on either service consume part of the budget before any hop latency does.
- Running the connector in more than one Collector replica splits the join: if the client and server spans of one call reach different replicas, neither replica sees a pair and both count an unpaired span.
- Adding a high-cardinality span attribute to `dimensions` multiplies an already near-N² edge count, and the resulting series explosion appears in the Prometheus exporter rather than in the connector.
- `store.max_items` (default `1000`) drops spans independently of the TTL, so a burst of concurrent calls produces missing edges while `unpaired_spans_total` and latency both look unremarkable.
- An uninstrumented callee with none of the `virtual_node_peer_attributes` present yields no virtual node; the edge is absent from the graph rather than shown as an unnamed peer.
- Reading `request_failed_total` as the callee's error rate conflates the two halves: the counter increments when *either* span failed, including client-side failures the server never saw.
