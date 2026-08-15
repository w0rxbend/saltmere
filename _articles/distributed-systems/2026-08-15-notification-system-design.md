---
title: "Design a Notification System"
date: 2026-08-15
track: distributed-systems
summary: "A notification system is a queue-shaped funnel: one ingestion API, a preference and rate-limit filter, and per-channel workers speaking APNs, FCM, email, and SMS. The load-bearing details are idempotency keys, collapse identifiers, tiered retry topics with a dead-letter queue, quiet-hours deferral, and delivery tracking, because at-least-once transport plus a user-visible channel makes duplicates observable."
reading_time: 7
tags: [notifications, push, apns, fcm, dead-letter-queue, idempotency, system-design]
sources:
  - title: "Firebase docs — About FCM messages (collapsible messages, TTL)"
    url: "https://firebase.google.com/docs/cloud-messaging/concept-options"
  - title: "Apple Developer — Sending notification requests to APNs"
    url: "https://developer.apple.com/documentation/usernotifications/sending-notification-requests-to-apns"
  - title: "Uber Engineering — Building Reliable Reprocessing and Dead Letter Queues with Apache Kafka"
    url: "https://www.uber.com/blog/reliable-reprocessing/"
  - title: "Stripe — Designing robust and predictable APIs with idempotency"
    url: "https://stripe.com/blog/idempotency"
---

**Gist.** A notification system delivers one logical event to a user across several transports — Apple Push Notification service (APNs), Firebase Cloud Messaging (FCM), email, and Short Message Service (SMS) — each of which is an unreliable remote dependency with its own rate limits and error taxonomy. The mechanism is a durable funnel: an idempotent ingestion API, a policy filter, priority-partitioned queues, and per-channel worker pools whose failures fall through tiered retry topics into a dead-letter queue (DLQ). The cost is that the transport is at-least-once, so exactly-once appearance must be reconstructed by explicit deduplication state and provider-side collapse keys, and that state has to be sized, expired, and paid for.

```
producers (services, cron, campaigns)
        |  POST /notify {user, event, payload, idempotency_key}
        v
  Notification API ── dedupe (idempotency store)
        |
        v
  queue, partitioned by priority        <- transactional / high / bulk
        |
        v
  Preference & policy filter            <- opt-outs, quiet hours, rate caps
        |               \
        v                v (deferred: quiet hours, digests)
  per-channel queues    delay store (sorted set / delay queue)
   |      |      |      |
  APNs   FCM   email   SMS workers ── retry topics ── DLQ
        |
        v
  delivery-status events -> tracking store / analytics
```

## Ingestion: one API, priority classes, idempotency

Producers call a single endpoint with `(user_id, event_type, payload, idempotency_key)` and never with a raw device token; **token resolution is owned by the notification system**, because it is the component that learns from provider feedback which tokens are dead. The endpoint persists the request and returns `202` without waiting on any provider.

The queue is partitioned by **priority class** rather than being one first-in-first-out (FIFO) stream: *transactional* (one-time passcodes, security alerts), *high* (mentions, payment events), and *bulk* (digests, campaigns). Separate queues with dedicated worker pools per class prevent a campaign backlog from sitting ahead of a passcode; the starvation analysis is in [the priority-queue article](/articles/sys-patterns/2026-08-15-priority-queue-pattern-worker-pool).

Idempotency appears twice, and the two occurrences defend against different failures. **At ingestion**, the producer's key is stored and a replayed request returns the original result rather than creating a second notification — Stripe's documented pattern for retried API calls. **At consumption**, workers deduplicate again, because queue delivery is at-least-once: a worker that sends successfully and then dies before acknowledging will see the same message redelivered. The consumption-side claim must be keyed by `(notification_id, device_id)`, since one notification legitimately fans out to several devices.

### Implementation sketch (Scala)

The load-bearing idea is the claim-then-send ordering: the deduplication key is set atomically *before* the provider call, and released only on a retryable failure, so a crash between send and acknowledgement leaves the claim in place and suppresses the redelivery.

```scala
enum Outcome:
  case Sent, Duplicate, Retrying, Terminal

trait ClaimStore:
  /** Atomic set-if-absent with expiry; true when this caller won the claim. */
  def claim(key: String, ttl: FiniteDuration): Boolean
  def release(key: String): Unit

final case class Msg(notificationId: String, deviceId: String, attempt: Int)

/** Provider errors, already classified by the channel's HTTP client. */
class TerminalError extends Exception
class RetryableError extends Exception

class Worker(store: ClaimStore, send: Msg => Unit, publish: (String, Msg) => Unit):

  private val tiers = Vector("retry-1m", "retry-10m", "retry-1h")

  def handle(msg: Msg): Outcome =
    val key = s"sent:${msg.notificationId}:${msg.deviceId}"
    if !store.claim(key, 24.hours) then Outcome.Duplicate
    else
      try { send(msg); Outcome.Sent }
      catch
        case _: TerminalError =>
          Outcome.Terminal            // invalid token or opt-out: never retried
        case _: RetryableError =>
          store.release(key)          // claim must not outlive the failed send
          tiers.lift(msg.attempt) match
            case Some(topic) => publish(topic, msg.copy(attempt = msg.attempt + 1)); Outcome.Retrying
            case None        => publish("dlq", msg); Outcome.Terminal
```

The claim's time-to-live (TTL) bounds the deduplication window. A redelivery arriving after the TTL expires will send again, so the TTL must exceed the maximum retention and retry horizon of the upstream queue.

## Preferences, rate limits, quiet hours

One filter service stands between the priority queues and the channel queues and answers a single question: may this notification reach this user on this channel now? It evaluates **opt-outs per (user, channel, event-category)**, consent and legal flags (marketing consent, SMS `STOP`), **per-user rate caps** enforced by a sliding-window counter, and **quiet hours in the user's timezone**.

Quiet hours and digest windows are **deferrals, not drops**: the message is parked keyed by `deliver_at` and released when that time arrives ([delayed-delivery mechanics](/articles/sys-patterns/2026-08-13-delayed-messages-job-scheduling)). Treating them as drops silently loses notifications whose value outlives the window. The transactional class bypasses caps and quiet hours.

## Channel workers

Each channel gets its own queue and worker pool because throughput ceilings, error taxonomies, and expiry controls differ.

| Channel | Protocol | Collapse/expiry lever | Characteristic failure |
|---|---|---|---|
| APNs (iOS) | HTTP/2, JSON Web Token auth, ~4 KB payload | `apns-collapse-id`, `apns-expiration`, `apns-priority` | `410 Unregistered` → prune token |
| FCM (Android/Web) | HTTP v1 API | `collapse_key`, TTL 0–28 days | `UNREGISTERED` → prune; quota errors |
| Email | SMTP / SES / SendGrid | none (digesting instead) | bounces, sender-reputation damage |
| SMS | Twilio and equivalents | none | per-number throughput caps, per-message cost |

Two mechanisms carry most of the weight. **Collapsing**: for state-summary notifications such as "N new messages", only the latest instance matters, so a collapse identifier instructs the platform to replace the undelivered older message rather than stack instances. Firebase documents that **FCM allows at most four different collapse keys per device at any one time**, and discards the extras unpredictably once that limit is exceeded, and that a TTL of 0 means the message is delivered immediately or dropped. **Token hygiene**: an APNs `410 Unregistered` response or an FCM `UNREGISTERED` error is a durable fact about the token, and must flow back into the token store as a deletion; otherwise quota is consumed by sends that cannot arrive and delivery metrics are permanently skewed.

**Template rendering** happens in the worker: `(event_type, channel, locale)` selects a versioned template that is hydrated with the payload. Producers therefore emit structured data rather than rendered prose, which keeps localisation and copy changes inside the notification system instead of requiring a redeploy of every producing service.

## Retries, dead-letter queue, and tracking

Requeueing a failed message onto its original topic causes head-of-line blocking and tight retry loops. Uber's Kafka reprocessing design routes failures into **a sequence of retry topics**, each consumed by a processor that waits that tier's delay before reprocessing, so the delay grows as a message advances through the tiers (`retry-1m`, `retry-10m`, `retry-1h` in the sketch above are illustrative names, not Uber's). It parks messages that exhaust the tiers in a **dead-letter queue** with the error attached, so they can be re-driven after a fix. The classification matters as much as the topology: **retryable** failures (5xx responses, timeouts, quota exhaustion) advance a tier, while **terminal** failures (invalid token, opted out) record a failure status directly and never enter the DLQ, where they would otherwise accumulate as permanent noise and hide real incidents behind DLQ-depth alerts.

Every state transition emits an event — `enqueued → filtered/deferred → rendered → sent → delivered → opened/clicked` — into a tracking store keyed by `notification_id` (a wide-row store such as Cassandra fits the access pattern) plus a stream to analytics. Provider webhooks carrying delivery receipts and bounces are what advance a record from `sent` to `delivered`; without them the system only knows what it handed to the provider.

**Broadcast fan-out** to a large segment must not arrive as one API call per recipient. A fan-out job reads the segment in batches, applies the same preference filter, checkpoints per batch so an interrupted job resumes rather than restarts, and drips into the *bulk* queues at a controlled rate, which is what keeps it from displacing transactional traffic.

Back-of-envelope sizing: 50M users at 4 notifications per day averages roughly 2,300 sends per second; a campaign compressing 50M sends into an hour is roughly 14k per second on the bulk tier alone. Workers are input/output-bound HTTP clients holding many sends in flight, so the binding constraints are provider rate limits and the throughput of the deduplication store, not worker CPU.

## Pitfalls

- **Deduplication keyed by `notification_id` alone** suppresses legitimate sends to a user's second and third devices; the key must include the device identifier.
- **Setting the deduplication claim after a successful send** re-sends on redelivery, because a crash in the window between provider acknowledgement and queue acknowledgement leaves no claim behind.
- **A claim TTL shorter than the queue's retention plus retry horizon** allows a late redelivery to pass the deduplication check and send a duplicate.
- **Routing terminal failures into the DLQ** inflates DLQ depth with unfixable messages, so the depth alert stops distinguishing incidents from ordinary invalid tokens.
- **Discarding provider unregistration responses** (`410 Unregistered`, `UNREGISTERED`) leaves dead tokens in the store, consuming quota on sends that cannot be delivered and depressing measured delivery rates indefinitely.
- **Treating quiet hours as a drop rather than a deferral** silently loses notifications whose relevance outlasts the quiet window.
- **Relying on collapse identifiers for unbounded coalescing** fails once more than four distinct collapse keys are in use for an FCM device, at which point FCM discards extras without a documented rule for which survive.
- **Retrying on the source topic** blocks every later message in the partition behind the failing one for the duration of the backoff.
- **Rendering prose in the producer** puts template and locale changes behind a redeploy of each producing service.
