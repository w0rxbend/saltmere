---
title: "OpenTelemetry Logs: Correlating Logs with Traces via the Bridge API"
date: 2026-08-10
track: observability
summary: "Unlike metrics and traces, OTel logs are built to wrap the logging library you already have. Here's the LogRecord data model, why you never call the Logs Bridge API directly, and a working Python appender that auto-stamps every log with the active span's trace_id and span_id."
reading_time: 6
tags: [opentelemetry, logs, otlp, traces, correlation, python, observability]
sources:
  - title: "Logs Data Model — OpenTelemetry Specification"
    url: "https://opentelemetry.io/docs/specs/otel/logs/data-model/"
  - title: "OpenTelemetry Logging (bridge overview) — Specification"
    url: "https://opentelemetry.io/docs/specs/otel/logs/"
  - title: "Logs Bridge API — opentelemetry-specification (bridge-api.md)"
    url: "https://github.com/open-telemetry/opentelemetry-specification/blob/main/specification/logs/bridge-api.md"
  - title: "Logs example — opentelemetry-python (docs/examples/logs/example.py)"
    url: "https://github.com/open-telemetry/opentelemetry-python/blob/main/docs/examples/logs/example.py"
  - title: "Specification Status Summary — OpenTelemetry"
    url: "https://opentelemetry.io/docs/specs/status/"
---

Metrics and traces arrived in OpenTelemetry as greenfield APIs: you instrument your code by calling `tracer.start_as_current_span(...)` or incrementing a counter, and there was rarely a prior standard to displace. Logs are different. Every service already logs — through `logging` in Python, Logback or Log4j in Java, `log/slog` in Go — and nobody wants to rewrite thousands of log statements. So OTel took the opposite design stance: **the logs signal is a bridge over your existing logging library, not a replacement for it.** That single decision explains almost everything that looks odd about the OTel logs API.

## The log data model

OTel first defines a vendor-neutral shape that any log line can be projected into. A **LogRecord** (data model status: **Stable**) has these fields:

- **Timestamp** — when the event occurred.
- **ObservedTimestamp** — when the collection layer observed it; falls back to `Timestamp` and is used when the original event time is unknown.
- **TraceId**, **SpanId**, **TraceFlags** — the trace-context fields. This is the correlation payload.
- **SeverityNumber** and **SeverityText** — a normalized numeric severity plus the original level string.
- **Body** — the log message itself (a string, or structured data).
- **Attributes** — key/value pairs specific to this event.
- **Resource** — what emitted the log (`service.name`, `k8s.pod.name`, …), shared across all telemetry from that process.
- **InstrumentationScope** and **EventName** round out the record.

`SeverityNumber` is the quiet workhorse. Every language and library spells levels differently — `WARNING` vs `WARN` vs `30`. OTel maps them onto a fixed ordinal scale so backends can filter consistently: **TRACE 1–4, DEBUG 5–8, INFO 9–12, WARN 13–16, ERROR 17–20, FATAL 21–24**. The four steps per band let a bridge preserve nuance (e.g. `WARN` = 13, `WARN3` = 15) while still sorting cleanly.

The three fields that make this article worth reading are **TraceId**, **SpanId**, and **TraceFlags**. From the spec: `TraceId` is the "Request trace ID as defined in W3C Trace Context ... Can be set for logs that are part of request processing," and "If SpanId is present TraceId SHOULD be also present." `TraceFlags` carries the W3C sampled flag. All three are optional — but when they are populated, a log line stops being an isolated string and becomes a node hanging off a specific span in a specific trace. That is what lets you click a span in Grafana/Tempo and jump straight to the logs Loki recorded for it.

## Why you don't call the Logs API directly

Here is the part that trips people up. OTel has a **Logs Bridge API**, and its spec (**Status: Stable**) is blunt about who it is for:

> "The API is not intended to be called by application developers directly. It is provided for logging library authors to build log appenders, which use this API to bridge between existing logging libraries and the OpenTelemetry log data model."

Contrast that with the Tracing API, which *is* your application-facing surface. For logs there is no ergonomic `otel_log.info("hello")` you sprinkle through your code. Instead the flow is:

1. You keep calling your normal logger (`logging.getLogger(__name__).error(...)`).
2. An **appender** (a.k.a. bridge) — a handler plugged into your logging library — receives each record.
3. The appender translates it into an OTel `LogRecord` via the Bridge API's `Logger.emit(...)`, filling in severity, body, attributes, **and the trace context from the currently active span**.
4. The **LoggerProvider** / SDK batches and exports those records over OTLP.

You configure the appender and SDK once at startup; individual log statements stay untouched. (The spec deliberately avoids baking the word "bridge" or "appender" into the API names, leaving room for a future user-facing logging API — but today, wrapping an existing library is the intended path.)

## Concrete setup: Python

Python's SDK ships a `LoggingHandler` that is exactly this appender for the stdlib `logging` module. Wire it up alongside a tracer:

```python
import logging
from opentelemetry import trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

resource = Resource.create({"service.name": "checkout"})

# Traces
trace.set_tracer_provider(TracerProvider(resource=resource))
trace.get_tracer_provider().add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(insecure=True))
)

# Logs: LoggerProvider + OTLP exporter
logger_provider = LoggerProvider(resource=resource)
set_logger_provider(logger_provider)
logger_provider.add_log_record_processor(
    BatchLogRecordProcessor(OTLPLogExporter(insecure=True))
)

# The appender: bridge stdlib logging -> OTel LogRecords
handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
logging.getLogger().addHandler(handler)

# --- application code, unchanged ---
tracer = trace.get_tracer(__name__)
log = logging.getLogger("checkout")

with tracer.start_as_current_span("charge-card"):
    log.error("gateway declined the card")   # <-- carries trace_id + span_id
```

The critical line is the last one. Because it runs inside `start_as_current_span`, the `LoggingHandler` reads the **active span from the current context** and stamps that record's `TraceId`, `SpanId`, and `TraceFlags` automatically. A log emitted *outside* any span simply leaves those fields empty. You changed no log statements — only startup wiring — and correlation is now free.

Note the `_logs` module underscore: the API is stable, but Python still exposes the SDK internals under a leading underscore in several releases. `OTLPLogExporter` pushes records to `localhost:4317` (or wherever `OTEL_EXPORTER_OTLP_ENDPOINT` points) — normally a Collector.

**Java** follows the same pattern with a named appender instead of a handler: add `OpenTelemetryAppender` from `io.opentelemetry.instrumentation.logback.appender.v1_0` to `logback.xml` (there's a Log4j2 equivalent), and it copies the active span context onto every event. Same idea, different logging ecosystem — which is the whole point of the bridge model.

## The Collector path

The appender sends OTLP logs to a Collector, which receives, processes, and re-exports them:

```yaml
receivers:
  otlp:
    protocols:
      grpc: { endpoint: 0.0.0.0:4317 }
      http: { endpoint: 0.0.0.0:4318 }

processors:
  batch: {}

exporters:
  otlphttp/loki:
    endpoint: http://loki:3100/otlp   # Loki's native OTLP endpoint

service:
  pipelines:
    logs:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlphttp/loki]
```

The `trace_id`/`span_id` fields survive this hop as first-class LogRecord attributes. In Loki they land as **structured metadata** (not labels — high cardinality), so in Grafana a `{service_name="checkout"} | trace_id="..."` query pulls exactly the logs for one trace, and Tempo's "Logs for this span" link works because both signals agree on the same IDs. Correlation is not a Grafana feature bolted on afterward; it is the trace context riding inside each LogRecord from the moment the appender created it.

## Why this matters

The payoff of the bridge design is that trace-log correlation costs you almost nothing at the code level. You do not adopt a new logging API, you do not thread trace IDs through function calls by hand, and you do not maintain a custom log formatter that greps the context. You install an appender, point an exporter at a Collector, and every log written inside a span is automatically joined to that span. When an alert fires on a span with an error, the logs are already sitting on it.

**Try next:** Run the Python snippet against a local Collector (`otel/opentelemetry-collector-contrib`) wired to Loki + Tempo + Grafana, emit one log inside a span and one outside it, then confirm in Grafana that only the in-span log carries a `trace_id` — and that clicking the span surfaces it.
