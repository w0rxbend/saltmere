---
title: "OpenTelemetry Baggage: Propagating Request-Scoped Context"
date: 2026-08-15
track: observability
summary: "traceparent records where a request has been; the baggage header carries what the edge learned about it — tenant identifier, synthetic-traffic flag, feature-flag variant. Baggage does not become span attributes on its own, it rides on every outbound call including calls to third parties, and the specification guarantees only 64 entries totalling 8192 bytes. This article covers the API, the span processor that makes baggage queryable, and the guardrails."
reading_time: 6
tags: [opentelemetry, baggage, propagation, context, w3c, tracing]
sources:
  - title: "W3C — Propagation format for distributed context: Baggage"
    url: "https://www.w3.org/TR/baggage/"
  - title: "OpenTelemetry — Baggage (concepts)"
    url: "https://opentelemetry.io/docs/concepts/signals/baggage/"
  - title: "OpenTelemetry specification — Baggage API"
    url: "https://opentelemetry.io/docs/specs/otel/baggage/api/"
  - title: "opentelemetry-processor-baggage (PyPI) — BaggageSpanProcessor"
    url: "https://pypi.org/project/opentelemetry-processor-baggage/"
  - title: "GHSA-rcgg-9c38-7xpx — Unbounded memory allocation in W3C Baggage propagation (opentelemetry-java)"
    url: "https://github.com/open-telemetry/opentelemetry-java/security/advisories/GHSA-rcgg-9c38-7xpx"
---

**Gist.** A service several hops downstream frequently needs a fact that only the edge of the system knew — which tenant issued the request, whether the traffic is synthetic, which experiment bucket applies — and threading that fact through every intermediate signature is impractical. The World Wide Web Consortium (W3C) `baggage` header solves this by carrying a small set of key–value pairs alongside `traceparent`, propagated automatically by instrumented clients and servers. The cost is that the payload is attacker-writable, is transmitted on **every** instrumented outbound call including calls to external vendors, consumes header bytes on every hop, and does not appear on spans unless a processor copies it there.

## The header

Baggage is defined by its own W3C specification, maintained alongside Trace Context. The wire format is a comma-separated list of `key=value` members, values percent-encoded, each member optionally carrying semicolon-delimited properties:

```
baggage: tenant.id=acme-corp,app.synthetic=true,flag.checkout=variant-b;expires=session
```

Conforming implementations must propagate at least **64 list members totalling 8192 bytes**. Beyond that floor an implementation is permitted to drop entries, which makes baggage a **bounded envelope rather than a payload channel**: a producer cannot assume any given entry survives an arbitrary number of hops. OpenTelemetry's default composite propagator is `tracecontext,baggage`, so standard software development kit (SDK) auto-instrumentation already serializes and deserializes the header on Hypertext Transfer Protocol (HTTP) clients and servers; the entry set is empty until something writes to it.

## Writing and reading baggage (Python)

The OpenTelemetry **Baggage API** is context-based rather than object-based. Setting a value does not mutate anything: it **returns a new immutable context** carrying the additional entry. The instrumented HTTP client serializes whatever entries are in the *active* context at the moment of the outbound call, so the write is only visible downstream if the new context has been attached to the current execution scope.

```python
from opentelemetry import baggage, context

def handle_login(request):
    ctx = baggage.set_baggage("tenant.id", resolve_tenant(request))
    ctx = baggage.set_baggage("app.synthetic",
                              str(is_synthetic(request)).lower(), context=ctx)
    token = context.attach(ctx)
    try:
        # every instrumented call below now carries the baggage header
        billing_client.get_invoices()
    finally:
        context.detach(token)

# ...five services later, no plumbing in between:
tenant = baggage.get_baggage("tenant.id")   # "acme-corp"
```

The `attach`/`detach` pair is the invariant that matters: **omitting `detach` leaks the entries into unrelated work** that later reuses the same execution context, which manifests as one tenant's identifier appearing on another tenant's requests.

Suitable cargo is low-cardinality and non-secret: **tenant or account identifier** (per-customer latency breakdowns), **synthetic-traffic flag** (excluding load tests from service-level-objective arithmetic), **feature-flag variant** (comparing error rates across an experiment), and the originating entry point or device class.

## Baggage is not span attributes

Baggage propagates in-band with the request, but the specification does **not** copy entries onto spans. A `tenant.id` entry can traverse six services and appear in zero traces. Bridging the two requires a **baggage span processor** — contributed packages exist for Python (`opentelemetry-processor-baggage`), Java, JavaScript, and Go (`baggagecopy`) — which stamps selected baggage entries onto each span as it starts:

```python
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.processor.baggage import BaggageSpanProcessor

provider = TracerProvider()
# predicate: only promote deliberately-namespaced keys, never ALLOW_ALL
provider.add_span_processor(
    BaggageSpanProcessor(lambda key: key.startswith(("tenant.", "app.", "flag.")))
)
```

A prefix predicate is preferable to the `ALLOW_ALL_BAGGAGE_KEYS` constant. **Baggage is caller-supplied input**, so an allow-all predicate lets any upstream — including an unauthenticated external caller — inject arbitrary attributes into the receiving service's telemetry.

The promotion runs in the **SDK, not the Collector**. By the time telemetry reaches the Collector as OpenTelemetry Protocol (OTLP) data, the baggage header no longer exists: it lived on the application's requests, not on the export pipeline. The Collector can still act as a backstop for attributes an over-broad predicate promoted:

```yaml
processors:
  attributes/scrub-baggage:
    actions:
      - key: user.email        # promoted by an over-eager predicate
        action: delete
      - key: session.token
        action: delete
service:
  pipelines:
    traces:
      processors: [attributes/scrub-baggage, batch]
```

### Implementation sketch (Scala)

The parsing side is where a receiving service enforces its own limits. The sketch below decodes a `baggage` header while applying the specification's floor as a hard cap, rather than allocating in proportion to whatever the caller sent.

```scala
final case class Entry(key: String, value: String)

object Baggage:
  val MaxMembers: Int = 64
  val MaxBytes: Int   = 8192

  /** Returns Nil for a header exceeding the size cap: no partial allocation. */
  def parse(header: String): List[Entry] =
    if header.length > MaxBytes then Nil
    else
      header
        .split(',')
        .iterator
        .take(MaxMembers)                      // bound the work before decoding
        .flatMap { member =>
          // properties after the first ';' are carried on the wire, not read here
          val pair = member.takeWhile(_ != ';').trim
          pair.indexOf('=') match
            case -1 => None
            case i  =>
              val k = pair.substring(0, i).trim
              val v = java.net.URLDecoder.decode(pair.substring(i + 1).trim, "UTF-8")
              if k.isEmpty then None else Some(Entry(k, v))
        }
        .toList

  def serialize(entries: List[Entry]): String =
    entries.iterator
      .take(MaxMembers)
      .map(e => s"${e.key}=${java.net.URLEncoder.encode(e.value, "UTF-8")}")
      .mkString(",")
```

The load-bearing line is `.take(MaxMembers)` placed **before** the percent-decoding step: the bound limits the decoding work rather than only the result. Applying it afterwards still decodes every member a hostile caller supplied. The split itself still touches the whole header, which is why the length check precedes it.

## Risks

| Risk | Mechanism | Guardrail |
|------|-----------|-----------|
| **Disclosure to third parties** | Instrumented HTTP clients attach `baggage` to *every* outbound call, including calls to payment processors, object stores and partner interfaces | Strip baggage at egress by configuring external clients with no baggage propagator; exclude personal data and secrets from baggage entirely |
| **Header growth** | Every hop transmits the bytes; proxies enforce header size caps | Few keys, short values; the specification floor is 64 entries / 8192 bytes |
| **Untrusted input** | Any caller can supply a `baggage` header | Validate before acting on entries; opentelemetry-java published advisory GHSA-rcgg-9c38-7xpx for unbounded memory allocation while parsing hostile baggage |
| **Cardinality echo** | Promoting a `user.id` entry to a span attribute reproduces the metrics-cardinality problem in span-derived metrics | Keep promoted keys bounded, under the same rule that governs metric labels |

Baggage is best modelled as **a postcard stapled to the request**: every party along the route can read it, forge it, or drop it. That makes it appropriate for routing hints and analysis dimensions, and inappropriate for authorization decisions or personal data.

## Pitfalls

- **Setting baggage without attaching the returned context.** `set_baggage` returns a new context and mutates nothing; the outbound header stays empty and downstream lookups return `None`.
- **Attaching a context without detaching it.** The token is never released, so entries persist into unrelated work on the same execution context and one request's tenant identifier is attributed to another's.
- **Expecting baggage to appear in traces.** Without a baggage span processor no entry reaches a span, and queries filtering on `tenant.id` return nothing while the header is demonstrably present on the wire.
- **Using `ALLOW_ALL_BAGGAGE_KEYS`.** Any caller, including an external one, can then write arbitrary span attributes; the telemetry backend receives attacker-chosen keys and the cardinality of the attribute set becomes unbounded.
- **Assuming entries survive every hop.** An implementation may drop members past the 64-entry / 8192-byte floor, so an entry set that is complete at the edge can be truncated by the time it reaches a deep service.
- **Relying on baggage for authorization.** The header is caller-supplied and unauthenticated, so a tenant identifier read from baggage is a claim, not a credential.
- **Scrubbing only in the Collector.** The Collector never sees the baggage header, only attributes already promoted by the SDK, so a leak into an outbound request to a third party is unaffected by any Collector processor.
