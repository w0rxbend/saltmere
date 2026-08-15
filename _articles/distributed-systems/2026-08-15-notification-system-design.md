---
title: "Design a Notification System"
date: 2026-08-15
track: distributed-systems
summary: "A notification system is a queue-shaped funnel: one ingestion API, a preference/rate-limit filter, and per-channel workers speaking APNs, FCM, email, and SMS. The interview points are the boring-sounding ones — idempotency keys, collapse IDs, retry topics with a DLQ, quiet-hours deferral, and delivery tracking — because at-least-once delivery plus a paging channel means duplicates are user-visible."
reading_time: 6
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

"Design a notification system" is really "design a multi-channel delivery pipeline with at-least-once semantics that *looks* exactly-once to the user." The shape is always the same funnel:

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

## Ingestion: one API, priorities, idempotency

Producers call one endpoint with `(user_id, event_type, payload, idempotency_key)` — never a raw device token; token resolution belongs to this system. Enqueue and return `202` immediately.

Partition the queue by **priority class**, not one big FIFO: *transactional* (OTP, security alert — seconds matter), *high* (mentions, payments), *bulk* (digests, marketing). Separate queues per class with dedicated worker pools is the robust version — the starvation math is in [the priority-queue article](/articles/sys-patterns/2026-08-15-priority-queue-pattern-worker-pool). An OTP must never queue behind 10M campaign sends.

Idempotency is two distinct defenses. At ingestion, dedupe on the producer's key (Stripe's pattern: store the key, return the original result on replay) so a retried API call doesn't create two notifications. At consumption, workers dedupe again, because queues deliver at-least-once:

```python
def handle(msg):
    key = f"sent:{msg.notification_id}:{msg.device_id}"
    if not redis.set(key, "1", nx=True, ex=86400):
        return ack(msg)                    # duplicate delivery: drop
    try:
        provider.send(render(msg))         # APNs / FCM / SES / Twilio
    except RetryableError:
        redis.delete(key)                  # release claim
        publish(retry_topic(msg.attempt + 1), msg)
    ack(msg)
```

## Preferences, rate limits, quiet hours

Before any channel queue, one filter service answers: *may this notification reach this user on this channel right now?* It checks **opt-outs per (user, channel, event-category)**, legal flags (marketing consent, SMS STOP), **per-user rate caps** ("max 5 pushes/hour, marketing 1/day") via a sliding-window counter, and **quiet hours** in the user's timezone. Quiet-hours and digest deliveries aren't drops — they're *deferrals*: park the message keyed by `deliver_at` and release it later ([delayed-delivery mechanics here](/articles/sys-patterns/2026-08-13-delayed-messages-job-scheduling)). Transactional class bypasses caps and quiet hours.

## Channel workers: where the platform details live

Each channel gets its own queue + worker pool, because failure modes, throughput, and rate limits differ wildly.

| Channel | Protocol | Collapse/expiry lever | Classic failure |
|---|---|---|---|
| APNs (iOS) | HTTP/2, JWT token auth, ~4 KB payload | `apns-collapse-id`, `apns-expiration`, `apns-priority` | `410 Unregistered` → prune token |
| FCM (Android/Web) | HTTP v1 API | `collapse_key` (max 4 stored per device), TTL 0–28 days | `UNREGISTERED` → prune; quota errors |
| Email | SMTP / SES / SendGrid | n/a (digesting instead) | bounces, spam-rate reputation |
| SMS | Twilio etc. | n/a | per-number throughput caps, cost |

Two details worth saying out loud. **Collapsing:** for "you have N new messages"-type updates only the latest matters — set a collapse ID and the platform replaces the undelivered older push instead of stacking 40 of them (FCM stores at most four collapsible messages per device; a TTL of 0 means deliver-now-or-drop). **Token hygiene:** feedback like APNs `410` must flow back to delete the device token, or you'll burn quota and skew metrics forever.

**Template rendering** happens in the worker: `(event_type, channel, locale) → versioned template`, hydrated with payload data. Producers send *data*, not prose — that's what lets you localize, A/B copy, and fix typos without redeploying twenty services.

## Retries, DLQ, and tracking

Blind requeue-on-failure causes head-of-line blocking and hot-loop retries. Uber's Kafka pattern is the standard answer: on failure, publish to a **tiered retry topic** (`retry-1m`, `retry-10m`, `retry-1h`) whose consumers delay before reprocessing; after N attempts, park the message in a **dead-letter queue** with the error attached, alert on DLQ depth, and re-drive after the bug is fixed. Distinguish *retryable* (5xx, timeout, quota) from *terminal* (invalid token, opted out) — terminal goes straight to a failure status, not the DLQ.

Every state change emits an event — `enqueued → filtered/deferred → rendered → sent → delivered → opened/clicked` — into a tracking store (Cassandra-style wide rows keyed by `notification_id`, plus a stream to analytics). This powers support debugging ("did user 42 get the OTP?"), provider webhooks (delivery receipts, bounces) close the loop from `sent` to `delivered`.

**Broadcast fan-out** ("announce to 50M users") must not enter the online path as 50M API calls: a fan-out job reads user segments in batches of ~10k, applies the same preference filter, and drips into the *bulk* queues at a controlled rate with a checkpoint per batch — resumable, and incapable of starving transactional traffic.

Back-of-envelope: 50M users × 4 notifications/day ≈ 2,300/s average; a marketing blast compresses 50M sends into an hour ≈ 14k/s on the bulk tier alone. Workers are I/O-bound HTTP clients (~hundreds of in-flight sends each), so tens of workers per channel suffice — the bottlenecks are provider rate limits and your own dedupe store, not CPU.

**Try next:** build the funnel on one Kafka topic + Redis: wire the idempotent consumer above, kill the worker mid-send, and verify the retry path re-delivers without double-sending; then add a `retry-1m` topic and a DLQ and force a poison message into it.
