---
title: "Pushing OTLP metrics straight into Prometheus 3: the receiver, and how attributes become labels"
date: 2026-08-04
track: observability
summary: "Prometheus 3 accepts OpenTelemetry metrics over OTLP directly at /api/v1/otlp/v1/metrics, without a Collector. How the receiver is enabled, how resource attributes and UTF-8 metric names are translated into the Prometheus data model, what delta temporality costs, and when a Collector remains necessary."
reading_time: 6
tags: [prometheus, opentelemetry, otlp, metrics, resource-attributes, observability]
sources:
  - title: "Using Prometheus as your OpenTelemetry backend (prometheus.io/docs/guides/opentelemetry)"
    url: "https://prometheus.io/docs/guides/opentelemetry/"
  - title: "Prometheus configuration reference — otlp block"
    url: "https://prometheus.io/docs/prometheus/latest/configuration/configuration/#otlp"
  - title: "Prometheus 3.0 release announcement"
    url: "https://prometheus.io/blog/2024/11/14/prometheus-3-0/"
  - title: "OpenTelemetry Collector — otlphttp exporter"
    url: "https://github.com/open-telemetry/opentelemetry-collector/tree/main/exporter/otlphttpexporter"
  - title: "Prometheus v3.13 release / download"
    url: "https://prometheus.io/download/"
---

**Gist.** OpenTelemetry Protocol (OTLP) metrics historically reached Prometheus only by being disguised as a scrape target or forwarded through remote-write, which required an OpenTelemetry Collector in the path. Prometheus 3 adds a native OTLP receive endpoint that ingests an `ExportMetricsServiceRequest` posted over HTTP. The cost is that two data models must be reconciled at the boundary — OTLP resource attributes against Prometheus labels, OTLP delta counters against Prometheus's cumulative assumption — and the receiver performs none of the buffering, batching or retry that a push pipeline otherwise supplies.

This article covers the receive path only: enabling it, and the translation applied to attributes and metric names once a payload crosses into the Prometheus data model. The behaviour described here is that of the Prometheus 3 line, current release **3.13**.

## Turning the receiver on

The endpoint is not enabled by default. It is opted into with a startup flag:

```
prometheus \
  --config.file=prometheus.yml \
  --web.enable-otlp-receiver
```

That exposes a single HTTP handler at `/api/v1/otlp/v1/metrics` on the normal web listener (`:9090`). **The doubled `v1` is not a typo**: `/api/v1` is the Prometheus API version and `/otlp/v1/metrics` is the OTLP metrics signal path. Only metrics are accepted; Prometheus is not a trace or log store, so no `/v1/traces` handler exists.

OTLP-over-HTTP defaults to a Protocol Buffers body, but the receiver also accepts JSON when the content type declares it, which permits a hand-constructed smoke test:

```bash
curl -X POST http://localhost:9090/api/v1/otlp/v1/metrics \
  -H "Content-Type: application/json" \
  -d '{"resourceMetrics":[{"resource":{"attributes":[
        {"key":"service.name","value":{"stringValue":"checkout"}}]},
      "scopeMetrics":[{"metrics":[{"name":"orders.total","unit":"1",
        "sum":{"aggregationTemporality":2,"isMonotonic":true,
          "dataPoints":[{"asInt":"5","timeUnixNano":"'$(date +%s)000000000'"}]}}]}]}]}'
```

A `200` with an empty body indicates ingestion. **`aggregationTemporality: 2` is cumulative**, which is the case the storage engine expects; the delta case is treated below.

## How OTLP maps onto the Prometheus data model

The two models disagree about where identity lives. In OTLP the **resource** — the entity that produced the telemetry — is a bag of attributes attached once per batch: `service.name`, `service.namespace`, `k8s.pod.name`, `cloud.region`. In Prometheus, every dimension is a label on a series, and the label set *is* the series identity. Something must decide which resource attributes are promoted to labels, and every promotion changes the series identity of the metrics beneath that resource.

By default Prometheus does **not** flatten every resource attribute onto every series. Instead it writes them into a single `target_info` metric, a gauge valued 1 carrying the resource attributes as labels and keyed by `job` and `instance`; PromQL joins against it when those dimensions are needed. The identifying attributes `service.namespace`, `service.name` and `service.instance.id` are collapsed into the `job` and `instance` labels Prometheus already understands. **The consequence of the default is that resource dimensions are queryable only through a join**, not by selecting on the metric alone.

Specific attributes are promoted to real labels by listing them under the `otlp` configuration block:

```yaml
otlp:
  promote_resource_attributes:
    - service.instance.id
    - service.name
    - service.namespace
    - service.version
    - deployment.environment
    - k8s.cluster.name
    - k8s.namespace.name
    - k8s.pod.name
  # Prometheus 3 default is UnderscoreEscapingWithSuffixes;
  # NoTranslation keeps UTF-8 names verbatim (requires the UTF-8 name scheme).
  translation_strategy: NoTranslation
  keep_identifying_resource_attributes: true
```

`promote_resource_attributes` is the load-bearing knob, and **each promoted attribute multiplies onto every series emitted by that resource** — an attribute such as `k8s.pod.name` carries the churn rate of pod scheduling into the cardinality of every metric the pod produces. `keep_identifying_resource_attributes: true` retains `service.name`, `service.namespace` and `service.instance.id` on `target_info` after they have been folded into `job` and `instance`, so the pre-collapse values remain readable.

## UTF-8 names: the dots-to-underscores mangling is gone

Prometheus 2 constrained metric names to `[a-zA-Z_:][a-zA-Z0-9_:]*` and label names to the same pattern without the colon. An OTLP metric named `http.server.request.duration` was therefore mangled to `http_server_request_duration`, with unit and type suffixes appended. Prometheus 3 supports UTF-8 names, and `translation_strategy` under `otlp` selects how much translation remains:

- `UnderscoreEscapingWithSuffixes` — the Prometheus 2 behaviour and still the default: dots become underscores, and `_total` and unit suffixes are added. Required when existing dashboards and recording rules already reference the mangled names.
- `NoUTF8EscapingWithSuffixes` — dots are preserved, suffixes are still added.
- `NoTranslation` — `http.server.request.duration` is stored exactly as sent, with neither escaping nor suffixes.

Preserving dots forces PromQL to quote the name: `{"http.server.request.duration"}`, and inside a selector with additional matchers, `{"http.server.request.duration", job="checkout"}`. **The strategy is not retroactive**: series already written under one strategy keep the names they were written with, so a switch splits a metric's history into two differently named series. That makes the choice effectively one-way once a body of dashboards exists.

## Delta temporality is the sharp edge

Prometheus is a cumulative system: a counter is monotonic and `rate()` derives the per-second change from the difference between samples, treating a decrease as a counter reset. OTLP counters may instead be **delta** — each data point reports the change since the previous export rather than a running total. Ingesting raw deltas therefore produces a series that repeatedly falls, which `rate()` interprets as a reset on nearly every sample, and the resulting rate is not the true rate.

Prometheus can convert on ingest, behind a separate experimental flag:

```
prometheus --web.enable-otlp-receiver \
           --enable-feature=otlp-deltatocumulative
```

The conversion is stateful: reconstructing a cumulative total requires remembering the running sum per series, and with the flag enabled that state lives in the Prometheus process. Emitting cumulative at the source avoids the state entirely — the SDK's temporality preference can be configured, or a `deltatocumulative` processor can run in a Collector, where the state is held in one place rather than in the storage engine.

Because OTLP arrives by push rather than on the scrape schedule, samples are not guaranteed to be timestamp-ordered on arrival, so **`storage.tsdb.out_of_order_time_window` generally has to be set** or late points are rejected by the head block.

## Pointing a producer at it

From an SDK, the standard OTLP environment variables suffice. The endpoint variable takes a **base** URL, to which the `/v1/metrics` signal path is appended:

```bash
export OTEL_METRICS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_METRICS_ENDPOINT=http://prometheus:9090/api/v1/otlp
export OTEL_SERVICE_NAME=checkout
```

From a Collector, the `otlphttp` exporter's `metrics_endpoint` is a full signal URL rather than a base, so the path is written out in full:

```yaml
exporters:
  otlphttp/prom:
    metrics_endpoint: http://prometheus:9090/api/v1/otlp/v1/metrics
service:
  pipelines:
    metrics:
      receivers: [otlp]
      exporters: [otlphttp/prom]
```

## Direct push versus a Collector

Direct OTLP-to-Prometheus removes a process from the path: no scrape configuration, no Collector to operate. It fits a small self-contained service whose producer is trusted, whose temporality is already cumulative, and whose metrics fan out to no other backend.

A Collector remains necessary where buffering, batching and retry are required — **the Prometheus receiver performs none of them**, so a sender that fails to reach the endpoint loses the export unless the sender itself retries. It is also the place to centralise delta-to-cumulative conversion or attribute rewriting, to route the same metrics to Prometheus and to a traces or logs backend simultaneously, and to interpose between Prometheus and untrusted or bursty senders. The receiver removes the requirement for a Collector; it does not remove the reasons for running one.

The OTLP receive path is distinct from remote-write. Remote-write carries samples already in the Prometheus data model to a remote-write receiver; the OTLP endpoint accepts OpenTelemetry payloads and translates them on ingest. The two use different endpoints and different payload formats.

## Pitfalls

- **Posting to `/api/v1/otlp` from a Collector produces a 404.** The SDK environment variable takes a base URL and appends the signal path; the `otlphttp` exporter's `metrics_endpoint` does not, and needs the full `/api/v1/otlp/v1/metrics`.
- **`rate()` over a delta counter returns a value unrelated to the true rate.** Each data point carries only the change since the previous export, so the series does not increase monotonically and the drops are read as counter resets rather than as increments.
- **Late-arriving pushes vanish without an obvious error at the query layer.** A pushed sample older than the head block's accepted window is rejected on ingest unless `storage.tsdb.out_of_order_time_window` is configured.
- **Resource attributes appear nowhere on the metric series by default.** They are written to `target_info` instead, and are reachable only by a PromQL join on `job` and `instance` unless listed in `promote_resource_attributes`.
- **Promoting a high-churn attribute such as `k8s.pod.name` multiplies cardinality across every metric of that resource,** because the promoted label becomes part of each series' identity and a new pod name creates a new series for every metric.
- **Changing `translation_strategy` renames series going forward only.** Existing history stays under the old name, so a query written for one form silently covers only part of the time range.
- **A `200` with an empty body confirms acceptance, not correctness.** A payload with a misdeclared temporality or an unintended resource attribute is ingested exactly as sent.
