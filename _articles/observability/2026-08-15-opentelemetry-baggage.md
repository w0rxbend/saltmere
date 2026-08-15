---
title: "OpenTelemetry Baggage: Propagating Request-Scoped Context (Carefully)"
date: 2026-08-15
track: observability
summary: "traceparent tells every service where a request has been; the baggage header carries what you know about it — tenant ID, synthetic-traffic flag, feature-flag variant. But baggage is not automatically span attributes, it rides on every outbound call including ones to third parties, and the spec only guarantees 64 entries in 8 KB. Here's the API, the span processor that makes baggage queryable, and the guardrails." 
reading_time: 5
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

The earlier W3C trace context article covered `traceparent`: 55 bytes of routing metadata that stitch spans into a trace. Its sibling header, **`baggage`**, answers a different question. Trace context says *where this request has been*; baggage carries *what the edge learned about it* — the tenant, the experiment bucket, the "this is a load test" flag — so a service five hops deep can act on facts only the front door knew. Powerful, and the sharpest foot-gun in the propagation toolbox.

## The header, briefly

Baggage is its own W3C spec (a Candidate Recommendation, maintained alongside Trace Context). The format is a comma-separated list of `key=value` pairs, values percent-encoded, with optional semicolon-delimited properties:

```
baggage: tenant.id=acme-corp,app.synthetic=true,flag.checkout=variant-b;expires=session
```

Conforming implementations must propagate at least **64 list members** totalling **8192 bytes** — beyond that they're allowed to drop entries, so treat baggage as a small, bounded envelope, not a payload channel. OpenTelemetry's default propagator set is `tracecontext,baggage`, so if you're using standard SDK auto-instrumentation, baggage already flows through your HTTP clients and servers; you just haven't put anything in it.

## Writing and reading baggage (Python)

The OTel **Baggage API** is context-based: setting a value returns a new context, and the instrumented HTTP client serializes whatever is in the active context onto outbound requests.

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

Typical cargo, all low-cardinality and non-secret: **tenant/account ID** (per-customer latency breakdowns), **synthetic-traffic flag** (exclude load tests from SLO math), **feature-flag variant** (compare error rates across an experiment), originating **entry point or device class**.

## Baggage is NOT span attributes

This is the mistake everyone makes once: baggage propagates *in-band with the request*, but the spec deliberately does **not** copy it onto spans. Your `tenant.id` travels through six services and appears in zero traces unless something writes it there. The clean fix is a **baggage span processor** — contrib packages exist for Python (`opentelemetry-processor-baggage`), Java, JS, and Go (`baggagecopy`) — which stamps selected baggage entries onto every span at start:

```python
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.processor.baggage import BaggageSpanProcessor

provider = TracerProvider()
# predicate: only promote deliberately-namespaced keys, never ALLOW_ALL
provider.add_span_processor(
    BaggageSpanProcessor(lambda key: key.startswith(("tenant.", "app.", "flag.")))
)
```

Use a prefix predicate, not the tempting `ALLOW_ALL_BAGGAGE_KEYS` constant — with allow-all, any upstream (or any *caller*, since baggage is attacker-writable input) can inject arbitrary attributes into your telemetry.

Note where this runs: in the **SDK**, not the Collector. By the time telemetry reaches the Collector as OTLP, the baggage header is gone — it lived on your application's requests, not on the export pipeline. What the Collector *can* do is act as a backstop for attributes the processor promoted but shouldn't have:

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

## The dangers, concretely

| Risk | Mechanism | Guardrail |
|------|-----------|-----------|
| **Leaking to third parties** | Instrumented HTTP clients attach `baggage` to *every* outbound call — including Stripe, S3, partner APIs | Strip baggage at egress (propagate `none` on external clients), never put PII/secrets in it |
| **Header bloat** | Every hop pays the bytes; proxies enforce header size caps | Few keys, short values; spec floor is 64 entries / 8 KB |
| **Untrusted input** | Any caller can send you a `baggage` header | Validate before acting on it; opentelemetry-java shipped an advisory (GHSA-rcgg-9c38-7xpx) for unbounded memory allocation while parsing hostile baggage |
| **Cardinality echo** | Promoting `user.id` baggage to span attributes recreates the metrics-cardinality problem in span-derived metrics | Keep promoted keys bounded, same rule as metric labels |

The mental model that keeps you safe: baggage is a **postcard stapled to the request** — anyone along the route can read it, forge it, or drop it. Great for routing hints and analysis dimensions; never for authorization decisions or personal data.

Try next: set a single `app.synthetic=true` baggage entry in your load-test client, promote it with a baggage span processor, and add `app.synthetic!="true"` to your SLO queries — your error budget stops paying for your own load tests.
