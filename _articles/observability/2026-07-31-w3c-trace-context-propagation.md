---
title: "W3C Trace Context: How One traceparent Header Stitches a Distributed Trace"
date: 2026-07-31
track: observability
summary: "A distributed trace is held together by a single shared trace-id carried in the traceparent header across every hop. The header decoded field by field, the tracestate rules, the extract-on-ingress / inject-on-egress model, and how OpenTelemetry propagates it by default."
reading_time: 6
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

**Gist.** Services that participate in one distributed trace never consult a shared database to agree on which trace they belong to; the correlation must travel in-band with the request. W3C Trace Context solves this by defining **`traceparent`**, a fixed-width HTTP header whose 16-byte trace-id is minted once at the entry point and copied unchanged through every hop, with each service replacing only the 8-byte parent-id by its own span-id. The cost is that trace identity becomes a property of header plumbing: any hop that drops, rewrites, or fails to forward the header silently severs the trace into two disconnected fragments, and no backend can repair the break after the fact.

`traceparent` is a W3C Recommendation, published in February 2020. Because the format is a standard rather than a vendor convention, services instrumented by different tracing implementations can participate in one trace as long as each of them reads and rewrites the same header.

## The header, decoded

`traceparent` consists of four dash-separated fields in lowercase hexadecimal:

```
traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
             └┬┘ └───────────────┬──────────────┘ └───────┬──────┘ └┬┘
           version            trace-id                 parent-id  flags
```

| Field | Value | Meaning |
|---|---|---|
| version | `00` | Trace Context Level 1 wire format (2 hex digits, 1 byte) |
| trace-id | `4bf92f3577b34da6a3ce929d0e0e4736` | 16-byte identifier, **identical on every service in the trace** |
| parent-id | `00f067aa0ba902b7` | 8-byte span-id of the *calling* operation |
| trace-flags | `01` | bit field; the `0x01` **sampled** bit indicates the caller may have recorded this trace |

The **trace-id is the load-bearing field**. It is generated once, at the point where a request first enters an instrumented system, and then held constant across every subsequent hop. That shared 128-bit value is what permits a collector to group spans emitted independently by unrelated processes into one trace. At each hop the receiving service mints a *new* span-id for its own work but retains the trace-id, and emits its own span-id as the parent-id of any request it makes downstream. The parent-child chain is therefore expressed entirely in headers; no participant needs to know the shape of the trace beyond its immediate caller.

Two validity constraints matter operationally. **An all-zero trace-id or an all-zero parent-id is invalid**, so an implementation that zero-fills a missing value produces a header that conformant receivers must reject rather than trust. And Level 1 `trace-flags` defines only the sampled bit: `...-01` denotes that the caller may have recorded the trace, `...-00` that it did not. The specification is explicit that the bit is a hint from the immediate caller and not a guarantee that trace data exists anywhere. The remaining bits are reserved, so a receiver must mask rather than compare the byte for equality.

## tracestate, and its ordering rule

`traceparent` carries identity; **`tracestate`** carries vendor-specific data as a comma-separated list of key/value members:

```
tracestate: rojo=00f067aa0ba902b7,congo=t61rcWkgMzE
```

The list holds **up to 32 members**. Keys begin from a restricted lowercase character set, optionally in the multi-tenant form `tenant@system`; values are printable ASCII excluding comma and equals sign. The non-obvious rule is **ordering: the left-most member is the most recently updated**. A vendor that modifies its own entry moves that entry to the front of the list, so position encodes recency rather than arbitrary insertion order. Truncation to fit the member limit therefore discards the least recently touched entries.

The two headers also differ in failure handling. **A malformed `tracestate` is discarded; it does not invalidate trace identity.** A malformed `traceparent` invalidates the incoming context entirely, and the receiving service starts a new trace.

Level 2 of the specification, still at Candidate Recommendation stage rather than Recommendation, keeps the wire format identical and adds a `random-trace-id` flag (`0x02`) signalling that the right-most 7 bytes of the trace-id are random. Sampling systems can then hash on a portion of the identifier known to be random.

## Extract on ingress, inject on egress

Propagation reduces to two operations placed at the edges of every service.

On an **incoming** request the service *extracts*: parse `traceparent` and `tracestate` into a context object, reuse the trace-id, and treat the received parent-id as the parent of the new local span. On an **outgoing** request the service *injects*: serialize the current context back into the request headers so the downstream service continues the same trace. OpenTelemetry's default propagator is this W3C `tracecontext` codec, commonly composited with the `baggage` propagator, and each operation is a single call:

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

A hop can be simulated by hand, which demonstrates that the trace is stitched by the header rather than by the backend:

```bash
curl -H 'traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01' \
     https://service-b.internal/api
```

Service B extracts trace-id `4bf9…4736`, parents its handler span on `00f067aa0ba902b7`, and re-injects an updated `traceparent` carrying a new parent-id when it calls service C.

The older **B3** scheme from Zipkin encodes the same information across several headers (`X-B3-TraceId`, `X-B3-SpanId`, `X-B3-ParentSpanId`, `X-B3-Sampled`). OpenTelemetry ships a B3 propagator, and a composite propagator can accept both formats concurrently while services migrate.

### Implementation sketch (Scala)

The parser is the load-bearing piece: strict field widths, hex-only characters, and rejection of the all-zero identifiers.

```scala
final case class TraceParent(
    version: Int,
    traceId: String,
    parentId: String,
    flags: Int
):
  def sampled: Boolean = (flags & 0x01) != 0
  // next hop keeps traceId, replaces parentId with the local span-id
  def forHop(localSpanId: String): String =
    f"$version%02x-$traceId-$localSpanId-$flags%02x"

object TraceParent:
  private val Hex = "^[0-9a-f]+$".r
  private def hexOf(s: String, n: Int): Option[String] =
    Option.when(s.length == n && Hex.matches(s))(s)

  def parse(header: String): Option[TraceParent] =
    header.split('-') match
      case Array(v, t, p, f) =>
        for
          _   <- hexOf(v, 2) if v != "ff"
          tid <- hexOf(t, 32) if tid.exists(_ != '0')
          pid <- hexOf(p, 16) if pid.exists(_ != '0')
          fl  <- hexOf(f, 2)
        yield TraceParent(
          Integer.parseInt(v, 16), tid, pid, Integer.parseInt(fl, 16)
        )
      case _ => None
```

A failed `parse` means the context is dropped and a fresh trace-id is generated; it never means the request is rejected.

## Pitfalls

- **A proxy or gateway that strips unknown headers severs the trace.** Spans upstream and downstream of it carry different trace-ids, and the resulting fragments cannot be joined afterwards, because the correlation existed only in the discarded header.
- **Zero-filling a missing parent-id produces an invalid header.** An all-zero parent-id is explicitly invalid, so conformant receivers discard the whole context and start a new trace rather than parenting onto the placeholder.
- **Comparing `trace-flags` for equality with `01` misclassifies future flags.** Level 2 defines `0x02`; a sampled trace arriving as `03` fails an equality test but passes a mask against `0x01`.
- **Injecting without extracting produces orphan traces.** A service that starts a fresh span for every inbound request but still injects on egress emits well-formed downstream headers under a trace-id nobody upstream shares.
- **Assuming `tracestate` order is insertion order misreads recency.** The left-most member is the most recently updated, so a vendor reading its own entry from the tail may be reading a stale position after another vendor's update.
- **Truncating `tracestate` beyond 32 members drops entries silently.** No error surfaces at the receiving service; a vendor's routing or tenant data ceases to arrive.
- **A malformed `tracestate` does not break identity, but a malformed `traceparent` does.** Debugging a trace that splits at a specific hop should start with the `traceparent` field widths, not with the vendor list.
