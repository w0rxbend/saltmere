---
title: "The Claim Check Pattern: Keeping Large Payloads Out of the Broker"
date: 2026-07-31
track: sys-patterns
summary: "Message brokers cap payloads between a few hundred kilobytes and a megabyte. The Claim Check pattern stores the blob in object storage and passes only a reference through the queue: flow, cleanup, security, and trade-offs."
reading_time: 6
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

**Gist.** A message broker routes small records; every major broker enforces a maximum message size, so a multi-megabyte payload is rejected outright or degrades every other message sharing the same queue or partition. The **Claim Check** pattern — named *Store in Library* by Hohpe and Woolf — writes the payload to external object storage and publishes only an opaque reference, so the broker carries a few hundred bytes instead of the blob. The cost is a second storage system on the critical path: one extra write and one extra read per payload, a new failure mode when the store is unavailable, and a lifecycle problem, because the blob outlives the message that points at it.

## Broker size ceilings

- **Apache Kafka** — the broker setting `message.max.bytes` defaults to approximately **1 MB**, with a matching per-topic `max.message.bytes`. Raising it in isolation is not sufficient: the replica fetcher limit `replica.fetch.max.bytes` and the consumer fetch limits must be raised alongside it, otherwise a record accepted by the leader cannot be replicated or consumed.
- **Amazon Simple Queue Service (SQS)** — long capped at **256 KB**, since raised to **1 MiB**. The SQS Extended Client Library remains available and carries larger payloads by storing them in Amazon S3 — Claim Check shipped as a first-party library.
- **Azure Service Bus** — **256 KB** on the Standard tier; the Premium tier supports up to **100 MB** per message over the Advanced Message Queuing Protocol (AMQP).

A generous ceiling does not make large messages cheap. A single large record occupies broker memory, consumes replication bandwidth, and holds head-of-line position in a partition or queue, so it raises tail latency for the small messages behind it.

## The write and read flow

The pattern splits one publish into two steps on each side, and the ordering is load-bearing.

**Producer.** First write the blob to object storage under an opaque, unguessable key. Then publish a small message containing that key plus metadata: size, content type, and a checksum. **The storage write must complete before the message is published**; the reverse ordering permits a consumer to receive a reference to an object that does not yet exist.

**Consumer.** Read the message, extract the key, fetch the blob — preferably through a time-limited signed URL rather than broad bucket credentials — verify the checksum, and process.

The invariant the pattern maintains is one-directional: **every published reference points at an object that already exists, and objects may exist with no reference**. The surplus is orphaned storage, which a lifecycle rule reclaims; the deficit — a reference with no object — is a dangling pointer that no amount of retrying repairs.

### Implementation sketch (Scala)

The load-bearing decisions are the size threshold and the ordering of the two writes. `ObjectStore` and `Broker` stand in for whichever client library is in use.

```scala
def sha256(bytes: Array[Byte]): String =
  java.security.MessageDigest.getInstance("SHA-256")
    .digest(bytes).map("%02x".format(_)).mkString

enum Envelope:
  case Inline(bytes: Array[Byte], contentType: String)
  case ClaimCheck(bucket: String, key: String, contentType: String,
                  size: Int, sha256: String)

final class Publisher(store: ObjectStore, broker: Broker,
                      bucket: String, thresholdBytes: Int):

  def publish(payload: Array[Byte], contentType: String): Unit =
    val envelope =
      if payload.length <= thresholdBytes then
        Envelope.Inline(payload, contentType)
      else
        val key = s"uploads/${java.util.UUID.randomUUID()}"  // opaque, non-enumerable
        // storage write first: a reference must never outrun its object
        store.put(bucket, key, payload, contentType)
        Envelope.ClaimCheck(bucket, key, contentType, payload.length, sha256(payload))
    broker.send(encode(envelope))

def consume(store: ObjectStore, envelope: Envelope): Unit = envelope match
  case Envelope.Inline(bytes, _) => process(bytes)
  case Envelope.ClaimCheck(bucket, key, _, _, digest) =>
    val blob = store.get(bucket, key)
    require(sha256(blob) == digest, s"checksum mismatch for $key")
    process(blob)
```

The threshold branch matters because the pattern is not free: below the broker limit, an inline message costs only the broker hop, while a claim check adds a storage write on the producer and a storage read on the consumer.

## Lifecycle and cleanup

The blob outlives the message, so deletion has to be assigned to someone. Two strategies are available.

- **Synchronous** — the consumer deletes the object after processing succeeds. This bounds storage growth tightly, but a consumer that crashes between fetch and delete orphans the object, and a consumer that deletes before the broker acknowledges the message destroys the payload that redelivery will need.
- **Asynchronous** — storage expires the object. An S3 object-lifecycle rule with a time to live (TTL) reclaims the object regardless of consumer fate, which decouples cleanup from the message workflow.

The constraint linking the two is that **the TTL must exceed the maximum redelivery window**, including dead-letter inspection time. A TTL shorter than the retry horizon converts a transient consumer failure into permanent data loss: the message returns to the queue, the object is already gone, and every subsequent attempt fails identically.

## Idempotency and security

**Idempotency.** Most brokers deliver at least once, so the same claim check can arrive more than once. Because the reference is immutable and the key is never reused, redelivery is idempotent at the fetch layer — the second delivery reads the same bytes and verifies the same checksum. Idempotency of the *side effects* is a separate obligation and requires deduplication on a message identifier before committing.

**Security.** The payload leaves the broker, so access control moves to the store.

- Issue **pre-signed URLs (S3) or shared access signature (SAS) URLs (Azure)** scoped to a single object with a short expiry, rather than distributing bucket-wide credentials.
- Keep keys **opaque** — a UUID rather than `user-42/invoice.pdf` — so a leaked reference discloses nothing about the object and neighbouring keys cannot be enumerated by guessing.
- A consequence of moving the payload out of the message is that sensitive content never enters broker storage or broker logs; it is exposed instead wherever the object store is exposed.

## Trade-offs

- **Extra round trips per payload.** Each large message costs one storage write and one storage read on top of the broker hop, which is why the threshold branch exists.
- **Two systems that must stay consistent.** The checksum detects a corrupted or replaced object; the lifecycle rule bounds orphan accumulation. Neither repairs a dangling reference.
- **Object storage becomes a dependency of message processing.** Consumers cannot complete while the store is unavailable, so the availability of the pipeline is bounded by the lower of the two, not by the broker alone.

## Pitfalls

- **Publishing the message before the storage write commits.** A consumer fetches the key, receives a not-found error, and the message is retried or dead-lettered even though the producer succeeded moments later.
- **A lifecycle TTL shorter than the redelivery window.** The message is redelivered after the object expires; every retry fails with not-found and the payload is unrecoverable.
- **Deleting the object before the broker acknowledges the message.** The acknowledgement is lost, the message is redelivered, and the blob it references no longer exists.
- **Raising `message.max.bytes` on Kafka brokers without raising `replica.fetch.max.bytes`.** The leader accepts records that followers cannot fetch, so replication stalls on the oversized record.
- **Structured, guessable keys such as `user-42/invoice.pdf`.** A single leaked reference discloses the naming scheme, and other users' objects can be requested by constructing keys.
- **No checksum in the reference.** A truncated upload or an overwritten key is processed as valid input, and the corruption surfaces downstream rather than at the fetch.
- **Applying the pattern unconditionally.** Kilobyte messages that fit inline pay two extra storage operations each, adding latency and cost to the common case.
