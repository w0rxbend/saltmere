---
title: "OpenTelemetry Logs: Correlating Logs with Traces via the Bridge API"
date: 2026-08-10
track: observability
summary: "Unlike metrics and traces, the OpenTelemetry logs signal is specified as a bridge over an existing logging library. This article covers the LogRecord data model, the reason the Logs Bridge API is not an application-facing surface, and a Python appender that stamps every record with the active span's trace_id and span_id."
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

**Gist.** A log line and a trace span describing the same request are two disconnected records unless they share identifiers. OpenTelemetry (OTel) solves this by specifying its logs signal as a **bridge**: an appender installed into the existing logging library translates each native record into an OTel `LogRecord` and copies the **trace context of the currently active span** into the record's `TraceId`, `SpanId` and `TraceFlags` fields. The cost is that correlation holds only where the appender can observe an active span in the current context — records emitted outside a span, or on a thread or task the context did not propagate to, arrive with those fields empty and remain uncorrelated.

Metrics and traces entered OTel as greenfield application-facing interfaces: instrumentation means calling `tracer.start_as_current_span(...)` or incrementing a counter. Logging is not greenfield. Services already log through `logging` in Python, Logback or Log4j in Java, `log/slog` in Go. The OTel logs specification takes the corresponding position: **the logs signal wraps an existing logging library rather than replacing it.** That stance accounts for the shape of the API described below.

## The log data model

The specification first defines a vendor-neutral record shape into which any log line can be projected. A **LogRecord** (data model status: **Stable**) carries:

- **Timestamp** — when the event occurred.
- **ObservedTimestamp** — when the collection layer observed the event; where the original event time is unknown it stands in as an approximation of `Timestamp`.
- **TraceId**, **SpanId**, **TraceFlags** — the trace-context fields, and the entire correlation payload.
- **SeverityNumber** and **SeverityText** — a normalized numeric severity alongside the original level string.
- **Body** — the message itself, either a string or structured data.
- **Attributes** — key/value pairs specific to this event.
- **Resource** — the identity of the emitting entity (`service.name`, `k8s.pod.name`, and similar), shared across all telemetry from that process.
- **InstrumentationScope** and **EventName** complete the record.

`SeverityNumber` exists because level vocabularies differ across ecosystems: `WARNING`, `WARN` and a bare integer may all denote the same band. The specification maps them onto a fixed ordinal scale so that a backend can filter uniformly: **TRACE 1–4, DEBUG 5–8, INFO 9–12, WARN 13–16, ERROR 17–20, FATAL 21–24**. The four steps per band allow a bridge to preserve intra-band gradations — `WARN` at 13, `WARN3` at 15 — while the total order remains sortable.

The three fields that make correlation possible are **TraceId**, **SpanId** and **TraceFlags**. The specification states that `TraceId` is the "Request trace ID as defined in W3C Trace Context ... Can be set for logs that are part of request processing," and that "If SpanId is present TraceId SHOULD be also present." `TraceFlags` carries the trace flags as defined by W3C Trace Context, of which the sampled bit is the only one that specification defines. **All three are optional.** When populated, the log line ceases to be an isolated string and becomes a record attached to one span within one trace, which is the precondition for navigating from a span in a trace viewer to the log lines a log store recorded for it.

## Why the Logs API is not called directly

OTel defines a **Logs Bridge API** whose specification (**Status: Stable**) states its intended caller explicitly:

> "The API is not intended to be called by application developers directly. It is provided for logging library authors to build log appenders, which use this API to bridge between existing logging libraries and the OpenTelemetry log data model."

This differs from the Tracing API, which is an application-facing surface. There is no ergonomic per-statement logging call to distribute through application code. The flow instead has four stages:

1. Application code continues to call its normal logger, for example `logging.getLogger(__name__).error(...)`.
2. An **appender** — a handler installed into the logging library — receives each native record.
3. The appender translates that record into an OTel `LogRecord` through the Bridge API's `Logger.emit(...)`, populating severity, body, attributes, **and the trace context read from the currently active span**.
4. The **LoggerProvider** and SDK batch those records and export them over the OpenTelemetry Protocol (OTLP).

The appender and SDK are configured once at process startup; individual log statements are unmodified. Neither "bridge" nor "appender" appears in the API surface itself, which is `LoggerProvider`, `Logger` and `emit`. Wrapping an existing library is the path the current specification describes; whether OTel later adds an application-facing logging API is not settled by this document.

## Concrete setup: Python

The Python SDK ships `LoggingHandler`, an appender for the standard-library `logging` module. It is installed alongside a tracer:

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

The final statement is the load-bearing one. Because it executes inside `start_as_current_span`, the `LoggingHandler` reads the **active span from the current context** and stamps that record's `TraceId`, `SpanId` and `TraceFlags`. A log emitted outside any span leaves those fields empty; **the appender has no other source for them, so correlation is a property of context propagation rather than of the logging call**. No log statement changed — only startup wiring.

The leading underscore in `opentelemetry._logs` and `opentelemetry.sdk._logs` reflects that the Python packages expose these modules as private in several releases even though the specification is stable; module paths can therefore move between versions. `OTLPLogExporter` sends records to `localhost:4317` by default, or to whatever `OTEL_EXPORTER_OTLP_ENDPOINT` names — typically a Collector.

**Java** follows the same pattern with a named appender rather than a handler: `OpenTelemetryAppender` from `io.opentelemetry.instrumentation.logback.appender.v1_0` is declared in `logback.xml`, with a Log4j2 equivalent available, and it copies the active span context onto each event. The mechanism is identical; only the host logging ecosystem differs, which is what the bridge model is for.

## The Collector path

The appender exports OTLP logs to a Collector, which receives, processes and re-exports them:

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

The `trace_id` and `span_id` fields survive this hop as first-class LogRecord fields. In Loki they are stored as **structured metadata rather than labels**, since trace identifiers are high-cardinality and labels form the index. A Grafana query of the form `{service_name="checkout"} | trace_id="..."` then selects the logs belonging to a single trace, and a trace viewer's per-span log link resolves because both signals carry identical identifiers. **Correlation is not a query-layer join heuristic; it is the trace context carried inside each LogRecord from the moment the appender constructed it.**

## Pitfalls

- **A log line emitted outside an active span has empty `TraceId` and `SpanId`.** The appender copies the context that is current at emission time and has no fallback; startup logs, background schedulers and shutdown hooks are the usual sources of uncorrelated records.
- **Context that does not propagate across a thread, executor or async task boundary silently drops correlation.** The log line is still exported, so the failure appears as missing rows in a trace-filtered query rather than as an error.
- **Attaching `trace_id` as a Loki label instead of structured metadata inflates the label index**, because every trace creates a distinct label value and therefore a distinct stream.
- **Adding `LoggingHandler` to a logger that also propagates to the root logger, which itself carries the handler, emits each record twice.** The duplicate carries the same trace context, so it is indistinguishable in a correlated view.
- **Calling the Logs Bridge API from application code contradicts its documented scope**; the specification designates it for logging-library authors building appenders, not for per-statement use in application code.
- **A severity mapping that collapses a level band to a single ordinal discards intra-band ordering.** Downstream filters expressed against the 1–24 scale then cannot distinguish, for example, `WARN` from `WARN3`.
- **`ObservedTimestamp` is not the event time.** Reading it as the event time reorders records whenever collection lags emission, such as after a batch export backlog.
