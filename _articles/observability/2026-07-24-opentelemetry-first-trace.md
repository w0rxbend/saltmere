---
title: "A first distributed trace: OpenTelemetry from zero to a flame graph"
date: 2026-07-24
track: observability
summary: "Metrics report that something is slow; traces report where. The smallest end-to-end setup that turns one request into a readable waterfall."
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

**Gist.** A latency metric reports that the 99th percentile doubled and a log reports that one request failed, but neither attributes the time to a component. A **trace** solves the attribution problem by recording each unit of work as a timed *span* and linking spans into a causal tree that survives process boundaries, carried by a propagated identifier. The cost is that the identifier must be threaded through **every** boundary the request crosses — HTTP calls, database drivers, queues, thread pools — and any boundary that drops it silently truncates the tree rather than raising an error.

## The three moving parts

1. **Software development kit (SDK).** In-process code that creates spans and joins them into a trace. A span holds a name, a start and end timestamp, a status, and a set of key/value **attributes**.
2. **Context propagation.** The mechanism that carries the trace identifier across a process boundary. Over HTTP, OpenTelemetry (OTel) defaults to the W3C Trace Context headers, so a span created in service B can name a span in service A as its parent.
3. **Collector.** A separate process that receives spans over the OpenTelemetry Protocol (OTLP) and forwards them to a backend such as Jaeger or Tempo. The application addresses only the collector, so changing backends is a collector configuration change rather than an application change.

## The identifier that makes the tree

W3C Trace Context defines the `traceparent` header as four hyphen-separated hex fields: a version, a **16-byte trace identifier**, an **8-byte parent span identifier**, and a one-byte set of trace flags whose least significant bit is the *sampled* flag. A companion `tracestate` header carries vendor-specific key/value data alongside it.

Two invariants follow directly from that encoding.

- **The trace identifier is constant for the whole trace.** Every span in the tree carries the same value; the parent span identifier is what changes at each hop. Reassembly at the backend is therefore a group-by on the trace identifier followed by a parent-pointer join, not an ordering problem.
- **The sampling decision travels with the request.** A downstream service reads the sampled flag out of the incoming header rather than deciding independently. If service A samples a request out and service B samples it in, the result is a fragment whose parent span was never exported — a **span whose parent identifier resolves to nothing**, which most user interfaces render as an orphan root.

## A minimal instrumented service

Automatic instrumentation supplies the boundary spans without source changes. In Python:

```bash
pip install opentelemetry-distro opentelemetry-exporter-otlp
opentelemetry-bootstrap -a install          # installs instrumentation for detected libraries
OTEL_SERVICE_NAME=checkout \
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \
  opentelemetry-instrument python app.py    # wraps Flask, requests, psycopg, ...
```

That produces a span for every inbound request and every outbound HTTP or database call, with context propagated between them. Domain work inside a handler is invisible to it and needs a manual span:

```python
from opentelemetry import trace
tracer = trace.get_tracer(__name__)

with tracer.start_as_current_span("price-basket") as span:
    span.set_attribute("basket.items", len(items))
    total = compute(items)
    span.set_attribute("basket.total", total)
```

Attributes are what makes the recorded spans queryable: **filtering traces by `basket.items > 100` selects the population whose latency is in question**, whereas a span name alone only selects the code path.

## Running a backend

```bash
docker run -d --name jaeger -p 16686:16686 -p 4317:4317 \
  jaegertracing/all-in-one:latest
```

The all-in-one image exposes the OTLP receiver on **4317** and the query user interface on **16686**, so a single container closes the loop from instrumented process to waterfall. It collapses the collector and the backend into one process; a standalone Collector is the configuration once more than one service exports.

## Asynchronous boundaries

Automatic instrumentation covers boundaries whose libraries ship instrumentation. A message queue is a boundary where the request context and the consumer execution are separated in time, and unless the trace context is written into the message on publish and read back on consume, the consumer starts a fresh trace. The observable symptom is **a producer trace that ends at the enqueue call and a consumer trace with no parent**, describing the same logical operation under two unrelated trace identifiers. OTel exposes propagators for this: an `inject` call writes the headers into a carrier the transport can hold, and an `extract` call rebuilds the parent context on the other side.

### Implementation sketch (Scala)

Propagation across a carrier the SDK does not know about — message headers as a plain map — reduces to supplying a setter and a getter.

```scala
import io.opentelemetry.api.OpenTelemetry
import io.opentelemetry.context.Context
import io.opentelemetry.context.propagation.{TextMapGetter, TextMapSetter}
import scala.jdk.CollectionConverters.*

final case class Message(body: Array[Byte], headers: Map[String, String])

class Propagation(otel: OpenTelemetry):
  private val propagator = otel.getPropagators.getTextMapPropagator

  // The setter mutates a carrier; a mutable builder stands in for the immutable Map.
  def inject(msg: Message): Message =
    val carrier = scala.collection.mutable.Map.from(msg.headers)
    val setter: TextMapSetter[scala.collection.mutable.Map[String, String]] =
      (c, key, value) => c.update(key, value)
    propagator.inject(Context.current(), carrier, setter)
    msg.copy(headers = carrier.toMap)

  def extract(msg: Message): Context =
    val getter = new TextMapGetter[Map[String, String]]:
      def keys(c: Map[String, String]): java.lang.Iterable[String] = c.keys.asJava
      def get(c: Map[String, String], key: String): String = c.get(key).orNull
    propagator.extract(Context.current(), msg.headers, getter)

// On consume: extract, then make the result current for the span that follows.
// val scope = propagation.extract(msg).makeCurrent()
```

The load-bearing part is the pairing: **whatever keys `inject` writes must survive the transport intact and be visible to `get`**. A broker that lowercases, strips, or truncates headers breaks extraction without any error, because a missing `traceparent` is indistinguishable from a request that legitimately starts a new trace.

## Pitfalls

- A span left unended never exports; the trace shows the parent and the siblings but not the work in question. Ending a span requires an exit path that runs on exceptions as well as on success.
- The process exits before the batching exporter flushes, and the final traces of a short-lived job are missing entirely. Shutting the tracer provider down explicitly is what forces the flush.
- Context is stored per execution context, so work handed to another thread or a callback loses the current span, and the child attaches to whatever context that thread happened to hold — commonly none.
- High-cardinality attributes such as a user identifier or a full URL with query parameters multiply storage and index size at the backend; the same field placed in a metric label rather than a span attribute multiplies time series.
- Independent sampling decisions at each service produce orphan spans, because the sampled flag arriving in `traceparent` is the decision and re-deciding discards the parent.
- Instrumenting only the outer boundary yields a waterfall whose largest bar is the handler itself, which locates the service but not the operation inside it.
