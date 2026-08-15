---
title: "Designing a Webhook Delivery System: Signatures, Retries, and Untrusted Endpoints"
date: 2026-08-15
track: microservices
summary: "A webhook provider operates a push-based message broker whose consumers are third-party HTTPS endpoints of unknown reliability. The load-bearing decisions: HMAC signatures over a timestamped payload, at-least-once delivery with backoff and per-endpoint isolation, an explicit refusal to guarantee ordering, and a policy for disabling endpoints that have failed for days. The consumer-side contract: verify, return 2xx quickly, process asynchronously, deduplicate by event id."
reading_time: 7
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

**Gist.** A webhook system inverts the usual consumer relationship: rather than consumers pulling from a broker, the provider pushes HTTPS POST requests to URLs supplied by customers, which may be slow, unreachable, misconfigured at the transport-layer-security (TLS) level, or answering with redirects. The mechanism that contains this is a durable event log fanned out into **one independent delivery queue per endpoint**, each retried with exponential backoff under a bounded window, and each request authenticated by a hash-based message authentication code (HMAC) over a timestamped body. The cost is that delivery becomes at-least-once and unordered: consumers must deduplicate, and neither side can assume an event reflects current state.

## Provider side: a queue problem in HTTP clothing

The shape on which Stripe, GitHub and Svix converge:

1. **Durable event log first.** An event (`payment_intent.succeeded`, `push`) is written to storage with a unique event id before any delivery is attempted. Delivery state is tracked **per (event, endpoint) pair**, not per event — a single event matching five subscriptions has five independent outcomes.
2. **Fan-out to per-endpoint queues.** Isolation per endpoint is the invariant: one customer's dead server must not head-of-line-block deliveries to any other customer. This is the [bulkhead argument](/articles/microservices/2026-07-26-timeouts-retries-bulkheads/) applied to outbound traffic.
3. **Short delivery timeout.** GitHub terminates the connection and counts the delivery as failed if the endpoint has not returned a 2xx status within **10 seconds**. Redirects and 4xx/5xx responses are failures as well — Stripe treats 3xx responses as errors and requires **TLS 1.2 or later**.
4. **Retries with backoff.** Delivery is at-least-once. Stripe retries in live mode "for up to three days with an exponential back off." Svix publishes its exact schedule — **immediately, then 5 s, 5 min, 30 min, 2 h, 5 h, 10 h, 10 h** — after which the message is marked `Failed`. Adding [jitter](/articles/microservices/2026-08-15-exponential-backoff-jitter-retry-storms/) prevents a widely subscribed endpoint returning from an outage from receiving a synchronised wave of retries whose attempts were all scheduled at the same instant.
5. **Per-endpoint circuit breaking and a disable policy.** Retrying every event indefinitely against a dead endpoint consumes workers and grows queues without bound. Per-endpoint health is tracked so that attempts can be suppressed early (a [circuit breaker](/articles/microservices/2026-07-24-circuit-breakers-resilience4j/) keyed per destination) and the endpoint eventually disabled. Svix's policy: if **all attempts to an endpoint fail for 5 days**, the endpoint is disabled and its owner is notified through an operational webhook. Manual **redelivery** — Stripe's dashboard resend, GitHub's redeliver API — is what lets a customer recover events after repairing the receiving side.
6. **No ordering guarantee.** Stripe's documentation states that events are not guaranteed to arrive in the order they were generated: `invoice.paid` can precede `invoice.created`. Guaranteeing global order requires serialising delivery per customer behind the slowest retry, which converts every retry into a stall for every later event. The contract that remains honest is that events are unordered notifications carrying identifiers, and consumers fetch current state from the application programming interface (API) when they need it. Offering ordering means per-endpoint first-in-first-out delivery with head-of-line blocking — a different product with a different cost.

## Signing: HMAC over timestamp and payload

An unauthenticated webhook endpoint accepts a forged `payment.succeeded` from anyone who discovers the URL. The common construction — Stripe's `Stripe-Signature`, GitHub's `X-Hub-Signature-256`, Svix's `svix-signature` — is an HMAC-SHA256 over the **raw request body** using a per-endpoint shared secret. Two details separate a correct scheme from a vulnerable one, and Svix's security write-up emphasises both:

- **The timestamp is signed together with the payload.** Stripe signs the string `"{t}.{body}"` and transmits `t=` in the header; verifiers reject a request whose timestamp falls outside a tolerance, which Stripe's libraries default to **5 minutes**. Without a signed timestamp, a captured request replays indefinitely: the body is unchanged, so the signature remains valid. Each retry attempt carries a fresh timestamp and therefore a fresh signature.
- **Multiple secrets are simultaneously valid.** Stripe keeps a rolled secret valid for **up to 24 hours**, and signatures are version-prefixed (`v1=`), so rotation does not drop events during the changeover window.

| | Stripe | GitHub | Svix |
|---|---|---|---|
| Signature header | `Stripe-Signature` (`t=`,`v1=`) | `X-Hub-Signature-256` | `svix-signature` + `svix-timestamp` |
| Replay defence | signed timestamp, 5 min default tolerance | delivery id + secret (no signed timestamp) | signed timestamp |
| Response deadline | fast 2xx required | 10 s | timeout then retry |
| Retry window | ~3 days, exponential backoff | manual redelivery API | published fixed schedule ending at 10 h, then `Failed` |
| Dedupe handle | `event.id` | `X-GitHub-Delivery` | message id |
| Dead endpoints | retries stop if endpoint disabled | — | auto-disable after 5 days of failures |

## Consumer side: verification and the receiving contract

Verification must operate on the **raw request bytes**. Any framework that parses the JavaScript Object Notation (JSON) body and re-serialises it may reorder keys or alter whitespace, which changes the HMAC input and fails an otherwise valid signature. Comparison must be constant-time, so that timing differences do not leak how many leading bytes of a forged signature were correct:

```python
import hashlib, hmac, time

def verify(secret: bytes, raw_body: bytes, ts: str, sig_hex: str,
           tolerance: int = 300) -> bool:
    if abs(time.time() - int(ts)) > tolerance:      # replay window
        return False
    expected = hmac.new(secret, f"{ts}.".encode() + raw_body,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_hex)   # constant-time
```

The remaining receiving obligations, as stated in the providers' own best-practice documentation:

- **Return 2xx before performing work.** Stripe instructs handlers to acknowledge the event immediately and to defer any complex logic until after the response; GitHub allows 10 seconds. Synchronous processing converts a delivery spike into a receiver outage, and the resulting 5xx responses into provider retries that arrive during that outage.
- **Process asynchronously.** The verified payload is enqueued and drained by workers at the receiver's own rate; a job that fails goes to the receiver's [dead-letter queue](/articles/microservices/2026-07-30-dead-letter-queues-poison-messages/) rather than back to the provider as a 500.
- **Deduplicate by event id.** At-least-once delivery makes duplicates contractual. Processed identifiers (Stripe's `event.id`, GitHub's `X-GitHub-Delivery`) are recorded behind a unique constraint — the mechanism of [idempotency keys](/articles/microservices/2026-07-30-idempotency-keys-safe-retries/), with the provider supplying the key.
- **Treat the webhook as a hint, not as the data.** Because ordering is not guaranteed, the event indicates *that* something changed; the object's current state is read from the API before acting.
- **Defence in depth.** Exempt the route from cross-site request forgery (CSRF) checks, restrict callers to the provider's published address ranges (GitHub's `/meta`, Stripe's IP list), and require HTTPS.

### Implementation sketch (Scala)

The delivery state machine per (event, endpoint) pair: a fixed backoff schedule, jitter, and exhaustion after the last entry.

```scala
import java.time.Instant
import scala.concurrent.duration.*
import scala.util.Random

enum DeliveryState:
  case Pending(attempt: Int, dueAt: Instant)
  case Delivered(at: Instant)
  case Failed(attempts: Int)

// Modelled on Svix's published schedule; index n is the wait before attempt n.
val schedule: Vector[FiniteDuration] =
  Vector(0.seconds, 5.seconds, 5.minutes, 30.minutes,
         2.hours, 5.hours, 10.hours, 10.hours)

/** Full jitter: sample uniformly in [0, base) so retries scheduled at the
  * same instant do not converge on the same wake-up time. */
def jittered(base: FiniteDuration): FiniteDuration =
  (Random.nextLong(base.toMillis max 1L)).millis

def next(s: DeliveryState.Pending, ok: Boolean, now: Instant): DeliveryState =
  if ok then DeliveryState.Delivered(now)
  else if s.attempt + 1 >= schedule.size then DeliveryState.Failed(s.attempt + 1)
  else
    val wait = jittered(schedule(s.attempt + 1))
    DeliveryState.Pending(s.attempt + 1, now.plusMillis(wait.toMillis))
```

The signature is `(state, outcome) => state` with no ambient effects, so a delivery worker crashing between the HTTP call and the state write repeats the attempt rather than losing it — which is the source of the at-least-once guarantee, and of the duplicates the consumer must absorb.

## Pitfalls

- **Verifying against a re-serialised body.** A handler that receives a parsed object and re-encodes it computes the HMAC over different bytes than the provider signed; valid deliveries are rejected with no signal distinguishing them from forgeries.
- **Omitting the timestamp from the signed string.** A signature over the body alone remains valid forever, so an attacker who captures one request can replay it any number of times.
- **Comparing signatures with `==`.** Byte-by-byte comparison that short-circuits on the first mismatch leaks the length of the correct prefix through response timing.
- **Rotating a secret with no overlap window.** Events signed with the old secret and still in the retry queue fail verification after the switch and are consumed as failures until the retry window expires.
- **Sharing one delivery queue across endpoints.** A single endpoint that holds connections open until the timeout occupies workers and delays every other customer's events — the head-of-line blocking that per-endpoint queues exist to prevent.
- **Retrying a dead endpoint without a disable policy.** Queue depth grows with the event rate for as long as the endpoint stays down, and the backlog is delivered as a burst when it returns.
- **Acting on the payload's embedded state.** Since ordering is not guaranteed, a later-arriving older event overwrites newer state unless the receiver refetches the object or compares versions.
- **Processing before deduplication.** A retry triggered by a receiver timeout arrives after the first attempt has already been processed; without a unique constraint on the event id, the side effect executes twice.
