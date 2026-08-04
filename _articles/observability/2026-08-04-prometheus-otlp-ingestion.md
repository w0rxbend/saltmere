---
title: "Pushing OTLP metrics straight into Prometheus 3: the receiver, and how attributes become labels"
date: 2026-08-04
track: observability
summary: "Prometheus 3 can accept OpenTelemetry metrics over OTLP directly at /api/v1/otlp/v1/metrics, no Collector required. Here's how to enable the receiver, how resource attributes and UTF-8 metric names get translated into the Prometheus data model, what to do about delta temporality, and when you should still route through a Collector instead."
reading_time: 5
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

For years the only way to get OpenTelemetry metrics into Prometheus was to make them look like a scrape target: run a Collector with a `prometheus` exporter, or use the Prometheus remote-write exporter and push. Prometheus 3 adds a third path that removes the middle layer entirely — it can receive OTLP directly. Your SDK or Collector does an HTTP POST of an `ExportMetricsServiceRequest` and Prometheus ingests it. This piece is only about that receive path: turning it on, and understanding what happens to your OTLP attributes and metric names once they cross the boundary into the Prometheus data model. (Current release at the time of writing is **3.13.2**, the 3.13 LTS line.)

## Turning the receiver on

The endpoint is not enabled by default. You opt in with a feature flag on startup:

```
prometheus \
  --config.file=prometheus.yml \
  --web.enable-otlp-receiver
```

That exposes a single HTTP handler at `/api/v1/otlp/v1/metrics` on the normal web listener (`:9090`). The doubled `v1` is not a typo: `/api/v1` is the Prometheus API version, `/otlp/v1/metrics` is the OTLP metrics signal path. Only metrics are accepted — Prometheus is not a trace or log store, so there is no `/v1/traces` here.

A quick smoke test with `curl`. OTLP-over-HTTP defaults to a protobuf body, but the receiver also accepts JSON if you set the content type, which is handy for a hand-rolled check:

```bash
curl -X POST http://localhost:9090/api/v1/otlp/v1/metrics \
  -H "Content-Type: application/json" \
  -d '{"resourceMetrics":[{"resource":{"attributes":[
        {"key":"service.name","value":{"stringValue":"checkout"}}]},
      "scopeMetrics":[{"metrics":[{"name":"orders.total","unit":"1",
        "sum":{"aggregationTemporality":2,"isMonotonic":true,
          "dataPoints":[{"asInt":"5","timeUnixNano":"'$(date +%s)000000000'"}]}}]}]}]}'
```

A `200` with an empty body means it was ingested. `aggregationTemporality:2` is cumulative — more on why that matters below.

## How OTLP maps onto the Prometheus data model

This is where the interesting behaviour lives, because OTLP and Prometheus disagree about how identity works. In OTLP, the **resource** (what produced the telemetry) is a bag of attributes attached once per batch: `service.name`, `service.namespace`, `k8s.pod.name`, `cloud.region`, and so on. In Prometheus, everything is labels on a series. Something has to decide which resource attributes become labels.

By default, Prometheus does **not** flatten every resource attribute onto every series — that would explode cardinality. Instead it writes them into a single `target_info` metric (a gauge valued 1) carrying all the resource attributes as labels, keyed by `job` and `instance`. You join to it in PromQL when you need those dimensions. The identifying attributes `service.namespace`, `service.name`, and `service.instance.id` are collapsed into the `job` and `instance` labels that Prometheus already understands.

If you want specific resource attributes promoted to real labels on the metrics themselves, you list them under the `otlp` config block:

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
  # Prometheus 3 default was UnderscoreEscapingWithSuffixes;
  # NoTranslation keeps UTF-8 names verbatim (requires the UTF-8 name scheme).
  translation_strategy: NoTranslation
  keep_identifying_resource_attributes: true
```

`promote_resource_attributes` is the knob most people actually want: keep the list short and deliberate, because each promoted attribute multiplies onto every series from that resource. `keep_identifying_resource_attributes: true` additionally keeps `service.name`/`service.namespace`/`service.instance.id` on `target_info` even after they've been folded into `job`/`instance`, so nothing is lost in the collapse.

## UTF-8 names: the dots-to-underscores mangling is gone

Prometheus 2 required metric and label names to match `[a-zA-Z_:][a-zA-Z0-9_:]*`, so an OTLP metric named `http.server.request.duration` was mangled to `http_server_request_duration`, and unit/type suffixes were appended. Prometheus 3 ships full UTF-8 name support, and the `translation_strategy` under `otlp` controls how much translation still happens:

- `UnderscoreEscapingWithSuffixes` — the classic behaviour, and still the default: dots become underscores, `_total`/unit suffixes are added. Choose this if your dashboards and recording rules already assume the mangled names.
- `NoUTF8EscapingWithSuffixes` — keep the dots, still add suffixes.
- `NoTranslation` — store `http.server.request.duration` exactly as sent, no escaping and no suffixes.

Preserving dots means PromQL has to quote the name: `{"http.server.request.duration"}` and, inside a selector, `{"http.server.request.duration", job="checkout"}`. UTF-8 storage keeps your OTLP semantic-convention names intact end to end, which is the whole point if you're standardising on OpenTelemetry naming — but it's a one-way door for existing queries, so decide before you have a year of dashboards built on the underscore form.

## Delta temporality is the sharp edge

Prometheus is a cumulative system: a counter only ever goes up, and `rate()` derives the per-second change. OTLP counters can be **delta** — each data point reports the change since the last export, not a running total. Feed raw deltas into Prometheus and `rate()` produces nonsense, because it sees a counter that keeps resetting.

Prometheus can convert on ingest, but it's behind a separate experimental flag:

```
prometheus --web.enable-otlp-receiver \
           --enable-feature=otlp-deltatocumulative
```

Even with that on, the cleaner fix is to emit cumulative in the first place — configure your SDK's temporality preference, or run a `deltatocumulative` (or `cumulativetodelta`'s inverse) processor in a Collector where the conversion state is centralised rather than living in Prometheus's memory. Because OTLP pushes arrive out of scrape order, you'll also usually want `storage.tsdb.out_of_order_time_window` set so late points aren't rejected.

## Pointing a producer at it

From an SDK, set the standard OTLP env vars — the endpoint is a **base** URL and the `/v1/metrics` signal path is appended automatically:

```bash
export OTEL_METRICS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_METRICS_ENDPOINT=http://prometheus:9090/api/v1/otlp
export OTEL_SERVICE_NAME=checkout
```

From a Collector, the `otlphttp` exporter points at the same base and Prometheus becomes the terminal backend:

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

## Direct push vs. going through a Collector

Direct OTLP-to-Prometheus is genuinely nice for small, self-contained services: one fewer moving part, no scrape config, no Collector to run. It's a good fit when the producing app is trusted, temporality is already cumulative, and you don't need to fan out to other backends.

Reach for a Collector when you need any of the buffering, batching, and retry that a real push pipeline requires (Prometheus's receiver does none of that for you), when you want delta-to-cumulative or attribute rewriting done in one central place, when you're routing the same metrics to Prometheus *and* a traces/logs backend, or when you want to shield Prometheus from untrusted or bursty senders. The receiver removes the requirement for a Collector; it doesn't remove the reasons you might still want one. And note the OTLP receive path is distinct from remote-write — remote-write is Prometheus-to-Prometheus replication, OTLP is OpenTelemetry-to-Prometheus ingestion.

**Try next:** start Prometheus with `--web.enable-otlp-receiver`, set `translation_strategy: NoTranslation` and one entry in `promote_resource_attributes`, then push the JSON `curl` above. Query `target_info` to see your resource attributes, then query your metric by its dotted UTF-8 name with `{"orders.total"}` and confirm the promoted label rode along.
