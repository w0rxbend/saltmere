---
title: "W3C Trace Context: How One traceparent Header Stitches a Distributed Trace"
date: 2026-07-31
track: observability
summary: "A distributed trace is held together by a single shared trace-id carried in the traceparent header across every hop. Here's the header decoded field by field, the tracestate rules, the extract-on-ingress / inject-on-egress model, and how OpenTelemetry propagates it by default."
reading_time: 5
tags: [observability, tracing, opentelemetry, w3c, context-propagation, http]
sources:
  - title: "Trace Context — W3C Recommendation (Level 1)"
    url: "https://www.w3.org/TR/trace-context/"
  - title: "Trace Context Level 2 — W3C Candidate Recommendation Draft"
    url: "https://www.w3.org/TR/trace-context-2/"
  - title: "OpenTelemetry — Context Propagation (concepts)"
    url: "https://opentelemetry.io/docs/concepts/context-propagation/"
  - title: "OpenTelemetry — Propagators API specification"
    url: "https://opentelemetry.io/docs/specs/otel/context/api-propagators/"
  - title: "OpenZipkin — B3 Propagation (header reference)"
    url: "https://github.com/openzipkin/b3-propagation"
---

A distributed trace looks like one connected flame graph, but the services that produced it never talked to a shared database to make that happen. What links a span in service A to a span in service C three hops away is one string, copied forward across every HTTP call: the **`traceparent`** header. Standardized by the W3C (a Recommendation since February 2020), it's the reason a trace from an OpenTelemetry app can flow through a Jaeger-instrumented service and still come out as a single trace.

## The header, decoded

`traceparent` is four dash-separated, lowercase-hex fields:

```
traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
             └┬┘ └───────────────┬──────────────┘ └───────┬──────┘ └┬┘
           version            trace-id                 parent-id  flags
```

| Field | Value | Meaning |
|---|---|---|
| version | `00` | Trace Context Level 1 wire format (2 hex, 1 byte) |
| trace-id | `4bf92f3577b34da6a3ce929d0e0e4736` | 16-byte ID, **identical on every service in the trace** |
| parent-id | `00f067aa0ba902b7` | 8-byte span-id of the *calling* operation |
| trace-flags | `01` | bit field; the `0x01` **sampled** bit means "this trace is recorded" |

The **trace-id** is the load-bearing field. It's minted once, at the entry point, and then held constant through every hop — that shared 128-bit value is precisely what lets a collector group the spans into one trace. At each hop the service mints a *new* parent-id/span-id for its own work but keeps the trace-id, and passes its span-id as the next request's parent-id. That's the parent-child chain, expressed entirely in headers. All-zero trace-ids or parent-ids are invalid, and `trace-flags` currently defines only the sampled bit — `...-01` recorded, `...-00` not.

## tracestate, and its ordering rule

`traceparent` carries the identity; **`tracestate`** carries vendor-specific baggage as a comma-separated key/value list:

```
tracestate: rojo=00f067aa0ba902b7,congo=t61rcWkgMzE
```

Up to **32 members**, keys lowercase-starting from a small charset (optionally `tenant@system` for multi-tenant vendors), values printable ASCII minus comma and equals. The subtle rule is **ordering: left is most-recently-updated.** A vendor that touches its entry moves it to the front, so position encodes recency. And unlike `traceparent`, a malformed `tracestate` is simply discarded — it never invalidates the trace identity.

Level 2 of the spec (a Candidate Recommendation as of March 2024) keeps the wire format identical and adds a `random-trace-id` flag (`0x02`) signalling that the right-most 7 bytes of the trace-id are random, which lets sampling systems hash on a known-random portion.

## Extract on the way in, inject on the way out

Propagation is two operations at the edges of every service. On an **incoming** request you *extract*: parse `traceparent`/`tracestate` into a context, reuse the trace-id, and make the received parent-id the parent of your new local span. On an **outgoing** request you *inject*: serialize the current context back into the headers so the downstream continues the same trace. OpenTelemetry's default propagator is exactly this W3C `tracecontext` codec (usually composited with `baggage`), and the two operations are one function call each:

```python
from opentelemetry import trace
from opentelemetry.propagate import inject, extract
import requests

tracer = trace.get_tracer(__name__)

# EGRESS: inject the current span's context as a traceparent header
def call_downstream(url: str):
    with tracer.start_as_current_span("client-request"):
        headers: dict[str, str] = {}
        inject(headers)                    # headers["traceparent"] = "00-...-...-01"
        return requests.get(url, headers=headers)

# INGRESS: extract remote context, then continue the trace
def handle_request(request_headers: dict[str, str]):
    ctx = extract(request_headers)
    with tracer.start_as_current_span("server-handler", context=ctx):
        ...  # shares the caller's trace-id; parent = caller's span-id
```

You can even fake a hop by hand to see it work:

```bash
curl -H 'traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01' \
     https://service-b.internal/api
```

Service B extracts trace-id `4bf9…4736`, parents its handler span on `00f067aa0ba902b7`, and re-injects an updated `traceparent` (new parent-id) when it calls C — one trace across three services.

If you're migrating from Zipkin, note the older **B3** scheme spreads the same information across multiple headers (`X-B3-TraceId`, `X-B3-SpanId`, `X-B3-ParentSpanId`, `X-B3-Sampled`). OpenTelemetry ships a B3 propagator, and you can run a *composite* propagator to accept both formats at once while services cut over.

**Try next:** Stand up two tiny HTTP services with OpenTelemetry auto-instrumentation, have A call B, and export to any collector. Then `curl` A directly with a `traceparent` you wrote by hand and confirm both spans land under *your* trace-id — proof that the trace is stitched by the header, not by the tracing backend.
