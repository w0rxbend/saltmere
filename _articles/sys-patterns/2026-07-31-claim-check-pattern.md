---
title: "The Claim Check Pattern: Keep Big Payloads Out of Your Queue"
date: 2026-07-31
track: sys-patterns
summary: "Message brokers cap payloads at kilobytes-to-a-megabyte. The Claim Check pattern stores the blob in S3 or blob storage and passes only a reference through the queue — here's the flow, cleanup, security, and trade-offs."
reading_time: 5
tags: [messaging, kafka, sqs, azure-service-bus, s3, architecture, integration-patterns]
sources:
  - title: "Store in Library (Claim Check) — Enterprise Integration Patterns, Hohpe & Woolf"
    url: "https://www.enterpriseintegrationpatterns.com/patterns/messaging/StoreInLibrary.html"
  - title: "Claim-Check pattern — Azure Architecture Center"
    url: "https://learn.microsoft.com/en-us/azure/architecture/patterns/claim-check"
  - title: "Amazon SQS message quotas — AWS Documentation"
    url: "https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/quotas-messages.html"
  - title: "Azure Service Bus quotas and limits — Microsoft Learn"
    url: "https://learn.microsoft.com/en-us/azure/service-bus-messaging/service-bus-quotas"
  - title: "Apache Kafka Message Size Limit: Best Practices & Config Guide — Confluent"
    url: "https://www.confluent.io/learn/kafka-message-size-limit/"
---

A message broker is a router, not a file server. Push a 40 MB video through it and you pay for it in broker memory, replication bandwidth, consumer buffer pressure, and — usually first — a hard rejection, because every broker caps message size.

The **Claim Check** pattern (Hohpe & Woolf call it *Store in Library*) is the fix: store the large payload in external storage, put only a small reference on the queue, and let the consumer fetch the blob on demand. The luggage-check analogy is exact — you hand over the bag, get a numbered ticket, and carry only the ticket.

## Why brokers cap message size

Small messages keep a broker fast: they fit in page cache, replicate cheaply, and don't stall consumers. So brokers enforce limits (verified July 2026):

- **Apache Kafka** — `message.max.bytes` defaults to **1 MB (1,048,576 bytes)** at the broker, with a matching per-topic `max.message.bytes`. You can raise it, but you must also bump `replica.fetch.max.bytes` and consumer `fetch.max.bytes` in lockstep, and large records erode throughput.
- **Amazon SQS** — historically **256 KB**, raised to **1 MiB (1,048,576 bytes)** in August 2025. The SQS Extended Client Library still exists to push up to **2 GB** via S3 — which is Claim Check as a first-party library.
- **Azure Service Bus** — **256 KB** on the Standard tier; Premium defaults to 1 MB and supports up to **100 MB** per message over AMQP.

Even where the ceiling is generous, big messages are a bad idea: they inflate p99 latency for every other message sharing the partition or queue.

## The write / read flow

**Producer (check the bag):**
1. Write the blob to object storage, keyed by an opaque, unguessable ID.
2. Publish a small message containing the key plus metadata (size, content type, checksum).

**Consumer (redeem the ticket):**
3. Read the message, pull the key.
4. Fetch the blob from storage — ideally via a time-limited signed URL — and process it.

The broker never sees the payload. The message stays a few hundred bytes.

```python
import boto3, json, uuid

s3, sqs = boto3.client("s3"), boto3.client("sqs")
BUCKET, QUEUE = "media-ingest", "https://sqs.us-east-1.amazonaws.com/123/ingest"

def publish_video(raw_bytes: bytes, content_type: str):
    key = f"uploads/{uuid.uuid4()}"                 # opaque, unguessable
    s3.put_object(Bucket=BUCKET, Key=key, Body=raw_bytes,
                  ContentType=content_type)
    # claim check: reference only, not the blob
    sqs.send_message(QueueUrl=QUEUE, MessageBody=json.dumps({
        "bucket": BUCKET, "key": key,
        "content_type": content_type,
        "size": len(raw_bytes),
        "sha256": _sha256(raw_bytes),               # for idempotent verify
    }))

def consume(msg):
    ref = json.loads(msg["Body"])
    blob = s3.get_object(Bucket=ref["bucket"], Key=ref["key"])["Body"].read()
    assert _sha256(blob) == ref["sha256"]           # integrity check
    process(blob)
```

## Lifecycle and cleanup

The blob outlives the message, so someone must delete it — otherwise storage leaks forever. Two strategies (per the Azure guidance):

- **Synchronous** — the consumer deletes the object after successful processing. Simple, but a lost/failed consumer orphans the blob, and retries need the object to still exist.
- **Asynchronous** — let storage expire it. On S3, an **object-lifecycle rule** or TTL (e.g. delete after 7 days) reclaims space regardless of consumer fate. This decouples cleanup from the message workflow and is the safer default for at-least-once queues.

Rule of thumb: use a TTL long enough to cover your maximum redelivery/retry window, and only add explicit deletion if storage cost demands it.

## Idempotency and security

**Idempotency** — most brokers deliver *at least once*, so the same claim check can arrive twice. Because the reference is immutable and the blob is content-addressable, redelivery is naturally safe: fetch the same key, verify the same checksum, dedupe on a message ID before committing side effects.

**Security** — the whole point is that the payload leaves the broker, so protect it at the store, not in transit:
- Hand out **pre-signed / SAS URLs** scoped to a single object and a short expiry (minutes), not broad bucket credentials.
- Keep keys **opaque** (UUIDs, not `user-42/invoice.pdf`) so a leaked reference reveals nothing and can't be enumerated.
- A side benefit noted by Hohpe & Woolf and Azure: sensitive data never touches broker storage or logs, tightening your access-control surface.

## Trade-offs

- **Extra round trip.** Every payload now costs a storage write and a storage read. Apply the pattern *conditionally* — send small messages inline, check the bag only when the payload is large. A common trick: put a size threshold in the producer and branch.
- **Two systems to keep consistent.** A blob with no message is orphaned storage; a message with no blob is a dangling pointer. TTLs and checksums are your guardrails.
- **Storage becomes a dependency of message processing.** If the object store is down, consumers can't complete — factor it into availability budgets.

The pattern trades a little latency and one more moving part for the ability to move arbitrarily large payloads through a broker that was never built to carry them.

**Try next:** Stand up LocalStack, create an SQS queue and an S3 bucket, and run the snippet above end to end with a 5 MB file. Then add an S3 lifecycle rule that expires `uploads/` after one day and confirm the object disappears while the (already-consumed) message does not.
