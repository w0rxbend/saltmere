---
title: "Designing a Webhook Delivery System: Signatures, Retries, and Endpoints You Can't Trust"
date: 2026-08-15
track: microservices
summary: "As a webhook provider you are running a push-based message broker where every consumer is someone else's flaky HTTPS endpoint. The load-bearing decisions: HMAC signatures over a timestamped payload, at-least-once delivery with backoff and per-endpoint isolation, explicitly refusing to guarantee ordering, and a policy for disabling endpoints that have been dead for days. Plus the consumer-side contract: verify, 2xx fast, process async, dedupe by event id."
reading_time: 6
tags: [webhooks, hmac, at-least-once, retries, event-delivery, api-design]
sources:
  - title: "Stripe docs — Receive Stripe events in your webhook endpoint"
    url: "https://docs.stripe.com/webhooks"
  - title: "GitHub docs — Best practices for using webhooks"
    url: "https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks"
  - title: "GitHub docs — Validating webhook deliveries"
    url: "https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries"
  - title: "Svix blog — Webhook Security"
    url: "https://www.svix.com/blog/webhook-security/"
  - title: "Svix docs — Retry schedule and endpoint disabling"
    url: "https://docs.svix.com/retries"
---

A webhook system inverts the usual consumer relationship: instead of consumers pulling from your broker, you push HTTPS POSTs to URLs your customers typed into a form. Those endpoints will be slow, down, behind misconfigured TLS, or returning 302s — and none of that is allowed to hurt your API or your other customers. That constraint drives the whole design.

## Provider side: it's a queue problem wearing an HTTP costume

The shape that Stripe, GitHub, and Svix all converge on:

1. **Durable event log first.** An event (`payment_intent.succeeded`, `push`) is written to storage with a unique event id before any delivery is attempted. Delivery state is tracked per (event, endpoint) pair.
2. **Fan-out to per-endpoint queues.** One event may match five subscriptions; each becomes an independent delivery job. Isolation per endpoint is the point: one customer's dead server must not head-of-line-block anyone else. This is the [bulkhead argument](/articles/microservices/2026-07-26-timeouts-retries-bulkheads/) applied outbound.
3. **Short delivery timeout.** GitHub terminates the connection and counts the delivery as failed if the endpoint hasn't returned 2xx within 10 seconds. Redirects and 4xx/5xx are failures too — Stripe explicitly treats 3xx responses as errors and requires TLS 1.2+.
4. **Retries with backoff.** Delivery is at-least-once. Stripe retries in live mode "for up to three days with an exponential back off." Svix publishes its exact schedule — immediately, 5s, 5m, 30m, 2h, 5h, 10h, 10h — then marks the message `Failed`. Add [jitter](/articles/microservices/2026-08-15-exponential-backoff-jitter-retry-storms/) so a popular endpoint coming back from an outage isn't greeted by a synchronized wave.
5. **Per-endpoint circuit breaking and a disable policy.** Retrying every event forever against a dead endpoint wastes workers and piles up queues. Track per-endpoint health; when failures persist, stop early attempts (a [circuit breaker](/articles/microservices/2026-07-24-circuit-breakers-resilience4j/), but keyed per destination) and eventually disable. Svix's policy: if all attempts to an endpoint fail for 5 days, the endpoint is disabled and the owner is notified via an operational webhook. Provide manual **redelivery** (Stripe's dashboard resend, GitHub's redeliver API) so customers can recover after fixing their side.
6. **Don't promise ordering.** Stripe's docs say it outright: events are not guaranteed to arrive in the order they were generated — `invoice.paid` can beat `invoice.created`. Guaranteeing global order would mean serializing delivery per customer behind the slowest retry. The honest contract: events are unordered notifications carrying ids; consumers fetch current state from the API if they need it. If you must offer ordering, it's per-endpoint FIFO with head-of-line blocking — an expensive different product.

## Signing: HMAC over timestamp + payload

An unauthenticated webhook endpoint is an open door: anyone who finds the URL can POST a fake `payment.succeeded`. The standard fix (Stripe's `Stripe-Signature`, GitHub's `X-Hub-Signature-256`, Svix's `svix-signature`) is an HMAC-SHA256 over the raw body using a per-endpoint shared secret. Two details separate a correct scheme from a vulnerable one, and Svix's security write-ups hammer both:

- **Sign a timestamp with the payload.** Stripe signs `"{t}.{body}"` and sends `t=` in the header; verifiers reject if the timestamp is outside a tolerance (Stripe's libraries default to 5 minutes). Without it, a captured request can be replayed later — same body, still-valid signature. Each retry attempt gets a fresh timestamp and signature.
- **Support key rotation.** Allow multiple active secrets (Stripe keeps an old secret valid for up to 24h after rolling; version-prefix the signatures, e.g. `v1=`) so customers can rotate without dropped events.

| | Stripe | GitHub | Svix |
|---|---|---|---|
| Signature header | `Stripe-Signature` (`t=`,`v1=`) | `X-Hub-Signature-256` | `svix-signature` + `svix-timestamp` |
| Replay defense | signed timestamp, 5 min default tolerance | delivery id + secret (no signed timestamp) | signed timestamp |
| Response deadline | fast 2xx required | 10 s | timeout then retry |
| Retry window | ~3 days, exponential backoff | manual redelivery API | 8 attempts over ~28 h, then `Failed` |
| Dedupe handle | `event.id` | `X-GitHub-Delivery` | message id |
| Dead endpoints | retries stop if endpoint disabled | — | auto-disable after 5 days of failures |

## Consumer side: the four-line contract

Verification must use the **raw request bytes** — any framework re-serialization of the JSON breaks the HMAC — and constant-time comparison:

```python
import hashlib, hmac, time

def verify(secret: bytes, raw_body: bytes, ts: str, sig_hex: str,
           tolerance: int = 300) -> bool:
    if abs(time.time() - int(ts)) > tolerance:      # replay window
        return False
    expected = hmac.new(secret, f"{ts}.".encode() + raw_body,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_hex)   # constant-time

# handler: verify -> enqueue(raw_body) -> return 200. Nothing else.
# worker: dedupe on event_id (unique index), then process.
```

The rest of the receiving contract, straight from the providers' own best-practice docs:

- **Return 2xx before doing work.** Stripe: return 200 *before* marking the invoice paid in your system; GitHub gives you 10 seconds. Synchronous processing turns a renewal-day delivery spike into your outage — and your 500s into their retry storm.
- **Process async.** Enqueue the verified payload and let workers drain it at your own rate; a failed job goes to your [dead-letter queue](/articles/microservices/2026-07-30-dead-letter-queues-poison-messages/), not back to the provider as a 500.
- **Dedupe by event id.** At-least-once means duplicates are contractual. Log processed ids (Stripe's `event.id`, GitHub's `X-GitHub-Delivery`) behind a unique constraint — the same trick as [idempotency keys](/articles/microservices/2026-07-30-idempotency-keys-safe-retries/), with the provider supplying the key.
- **Treat the webhook as a hint, not the data.** Since ordering isn't guaranteed, use the event to learn *that* something changed and fetch the object's current state from the API before acting on stale embedded payloads.
- **Defense in depth:** exempt the route from CSRF checks, pin the provider's published IP ranges (GitHub's `/meta`, Stripe's IP list), require HTTPS.

**Try next:** build a toy provider — Postgres event table, per-endpoint delivery jobs, the Svix retry schedule, HMAC over `timestamp.body` — point it at a consumer that randomly 500s and sleeps 15 s, and confirm one bad endpoint can't delay the others; then break your consumer's dedupe and watch double-processing appear.
