---
title: "Your first distributed trace: OpenTelemetry from zero to a flame graph"
date: 2026-07-24
track: observability
summary: "Metrics tell you something is slow; traces tell you where. Here's the smallest end-to-end setup that turns a request into a waterfall you can actually read."
reading_time: 5
tags: [opentelemetry, tracing, otel, spans, context-propagation]
sources:
  - title: "OpenTelemetry documentation"
    url: "https://opentelemetry.io/docs/"
  - title: "OpenTelemetry Collector"
    url: "https://opentelemetry.io/docs/collector/"
  - title: "W3C Trace Context"
    url: "https://www.w3.org/TR/trace-context/"
---

The three "pillars" framing of observability undersells traces. A metric says p99 latency doubled. A log says a specific request errored. A **trace** says: this request spent 4 ms in your handler, 190 ms waiting on the payments service, which spent 180 ms in a single SQL query. That causal waterfall is the thing that ends arguments in incident channels — and OpenTelemetry (OTel) is now the vendor-neutral default for producing it.

## The three moving parts

1. **SDK** in your service creates *spans* (timed units of work) and joins them into *traces*.
2. **Context propagation** carries the trace id across process boundaries — over HTTP it's the W3C `traceparent` header — so a span in service B knows it's a child of a span in service A.
3. **Collector** receives spans over OTLP and forwards them to a backend (Jaeger, Tempo, a vendor). Your app talks only to the collector; swapping backends never touches app code.

## A minimal instrumented service

Auto-instrumentation gets you 80% for free. In Python:

```bash
pip install opentelemetry-distro opentelemetry-exporter-otlp
opentelemetry-bootstrap -a install          # pulls instrumentation for your libs
OTEL_SERVICE_NAME=checkout \
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \
  opentelemetry-instrument python app.py     # wraps Flask, requests, psycopg, ...
```

That alone produces spans for every inbound request and outbound HTTP/DB call, with context propagated between them. For the parts that matter to *you*, add manual spans:

```python
from opentelemetry import trace
tracer = trace.get_tracer(__name__)

with tracer.start_as_current_span("price-basket") as span:
    span.set_attribute("basket.items", len(items))
    total = compute(items)          # your real work
    span.set_attribute("basket.total", total)
```

Attributes are the payoff: filtering traces by `basket.items > 100` to find the slow ones is what turns tracing from a demo into a debugging tool.

## Run the backend in one command

```bash
docker run -d --name jaeger -p 16686:16686 -p 4317:4317 \
  jaegertracing/all-in-one:latest
```

Point `OTEL_EXPORTER_OTLP_ENDPOINT` at it, hit your endpoint a few times, open `http://localhost:16686`, and there's your waterfall. The all-in-one image is the collector *and* the UI, so it's the fastest possible path to a first trace; graduate to a standalone Collector when you have more than one service.

## The one habit that makes traces worth it

Propagate context across **every** async boundary — message queues, background jobs, the ESP32-to-backend MQTT hop from the IoT track. A trace that stops at the queue is a trace with a hole in it. OTel has propagators for exactly this; inject the trace context into the message on publish and extract it on consume, and a single trace can span "sensor reading published" all the way to "row written." That is when observability stops being dashboards and starts being *cause and effect*.

**Try next:** add one manual span with two attributes to your hottest endpoint, generate load, and sort traces by duration in Jaeger. The slowest trace almost always shows you something the averages were hiding.
