---
title: "Exactly-Once in Kafka: What the Idempotent Producer and Transactions Actually Guarantee"
date: 2026-08-13
track: microservices
summary: "Exactly-once delivery is impossible over a lossy network, but exactly-once processing is not. How Kafka's idempotent producer (PID + sequence numbers) and transactions (transactional.id, read_committed) achieve it, where the guarantee stops, and why at-least-once plus an idempotent consumer is often the better answer."
reading_time: 5
tags: [kafka, exactly-once, transactions, delivery-semantics, idempotency]
sources:
  - title: "Exactly-once Semantics Are Possible: Here's How Kafka Does It (Confluent blog)"
    url: "https://www.confluent.io/blog/exactly-once-semantics-are-possible-heres-how-apache-kafka-does-it/"
  - title: "KIP-98 — Exactly Once Delivery and Transactional Messaging"
    url: "https://cwiki.apache.org/confluence/display/KAFKA/KIP-98+-+Exactly+Once+Delivery+and+Transactional+Messaging"
  - title: "KIP-679 — Producer will enable the strongest delivery guarantee by default"
    url: "https://cwiki.apache.org/confluence/display/KAFKA/KIP-679:+Producer+will+enable+the+strongest+delivery+guarantee+by+default"
  - title: "Exactly-once semantics with Kafka transactions (Strimzi blog)"
    url: "https://strimzi.io/blog/2023/05/03/kafka-transactions/"
  - title: "Message Delivery Semantics (Apache Kafka documentation)"
    url: "https://kafka.apache.org/documentation/#semantics"
---

"Does Kafka support exactly-once?" is a favorite interview trap because the honest answer is *yes, no, and it depends what you mean*. Untangling the three meanings — delivery, writing, processing — is the whole game.

## The three semantics

Delivery semantics fall out of one decision: what do you do when you don't get an acknowledgment?

- **At-most-once**: don't retry. The send may have been lost; you accept loss to guarantee no duplicates. (Consumer-side equivalent: commit offsets *before* processing.)
- **At-least-once**: retry until acked. The original may have succeeded and only the ack was lost, so you accept duplicates to guarantee no loss. (Consumer-side: process, *then* commit.)
- **Exactly-once**: every message affects the system precisely once.

Here's the trap: exactly-once **delivery** is impossible in the general case. The ack can always be lost after the message arrived — the Two Generals problem — so the sender can never distinguish "not delivered" from "delivered, ack lost." Any protocol must choose: give up (at-most-once) or resend (at-least-once). What *is* achievable is exactly-once **processing**: deliver at least once, but make redelivery have no additional effect. Everything Kafka does under the "exactly-once semantics" (EOS) banner, introduced by KIP-98 in Kafka 0.11 (2017), is a mechanized version of that move.

## Layer 1: the idempotent producer

A producer retry after a lost ack would classically write the batch twice. Kafka fixes this the way TCP fixes duplicate packets: identity plus sequence numbers.

- On startup the broker assigns the producer a **PID** (producer ID).
- Every batch carries a **per-partition, monotonically increasing sequence number**.
- The broker tracks the last sequence per (PID, partition) and silently drops a batch it has already appended; a gap raises `OutOfOrderSequenceException`.

Crucially, unlike TCP, the (PID, sequence) state is stored *in the replicated log itself*, so deduplication survives broker failover. Since Kafka 3.0 this is the default (`enable.idempotence=true`, `acks=all` — KIP-679); there is little reason to ever turn it off.

The limits matter for interviews: dedup is per **partition** and per **producer session**. Restart the application and it gets a new PID — the broker can no longer recognize its duplicates. And nothing spans multiple partitions or topics.

## Layer 2: transactions

Transactions extend the guarantee across partitions and across restarts. You give the producer a stable, unique `transactional.id`; the broker maps it to a PID plus an **epoch**. If a "zombie" instance (an old process presumed dead) tries to produce with the same `transactional.id`, its stale epoch gets it **fenced** with an error instead of corrupting the output.

The canonical use is the **consume-transform-produce loop**, where the consumed offsets are committed *inside* the transaction — offsets are just writes to `__consumer_offsets`, so input progress and output records commit or abort atomically:

```java
Properties p = new Properties();
p.put("bootstrap.servers", "kafka:9092");
p.put("transactional.id", "payments-enricher-0");   // stable per instance
p.put("enable.idempotence", "true");                 // implied, default since 3.0

producer.initTransactions();                         // fences zombies
while (true) {
  ConsumerRecords<K,V> in = consumer.poll(ofMillis(200));
  producer.beginTransaction();
  try {
    for (var rec : in) producer.send(transform(rec));
    producer.sendOffsetsToTransaction(offsetsOf(in), consumer.groupMetadata());
    producer.commitTransaction();                    // offsets + output, atomically
  } catch (Exception e) {
    producer.abortTransaction();                     // consumer re-reads the batch
  }
}
```

Downstream consumers must set `isolation.level=read_committed`; the broker then holds them at the **last stable offset (LSO)**, refusing to hand out records from still-open transactions. Kafka Streams wraps this entire dance into one config: `processing.guarantee=exactly_once_v2`. The Strimzi write-up documents the cost: transactional markers, extra RPCs, sequential transactions per producer — a real throughput hit, which is why it's opt-in.

| | At-most-once | At-least-once | Kafka EOS (transactions) |
|---|---|---|---|
| Loss possible | Yes | No | No |
| Duplicates possible | No | Yes | No (within Kafka) |
| Typical config | `acks=0`, no retries | `acks=all`, retries, commit after processing | `transactional.id` + `read_committed` |
| Cost | Cheapest | Consumer must tolerate dupes | Latency + throughput overhead |
| Use when | Metrics, telemetry | Almost everything | Multi-topic atomicity, stream aggregations |

## Where the guarantee stops

Kafka transactions cover **Kafka-to-Kafka** flows only. The moment your consumer calls a payment API, sends an email, or writes to Postgres, that side effect is outside the transaction: abort the transaction and the email is still sent; crash after the DB write but before the commit and the batch replays. There is no distributed transaction between Kafka and your database — bridging that gap in the *producing* direction is exactly what the transactional outbox pattern (covered earlier here) is for.

## The pragmatic alternative: at-least-once + idempotent consumer

For most services the interview-worthy answer is: don't chase EOS machinery — accept at-least-once and make the consumer idempotent, so duplicate delivery causes no duplicate effect. Same philosophy as idempotency keys for HTTP retries (covered earlier here), applied to events:

- Give every event a stable ID (or derive a natural key).
- Dedup with a unique constraint: `INSERT ... ON CONFLICT (event_id) DO NOTHING`, in the same DB transaction as the state change.
- Or make the handler a natural **upsert**: "set balance to X" survives replay; "add 10 to balance" does not.

This works across any transport (Kafka, SQS, webhooks), survives consumer restarts, and covers external side effects — the exact places Kafka transactions can't reach. Reserve real transactions for Kafka-in, Kafka-out pipelines where atomic multi-partition writes genuinely matter.

**Try next:** Take a consumer you own and kill it (`kill -9`) mid-batch, after processing but before committing offsets; count the duplicates on restart, then add an `ON CONFLICT DO NOTHING` dedup on the event ID and confirm the replayed batch changes nothing.
