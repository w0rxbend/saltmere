---
title: 'Delivery semantics: at-most-once, at-least-once, and the ''exactly-once'' myth'
date: 2026-08-10
track: distributed-systems
summary: 'Three delivery guarantees, defined precisely — and why interviewers want you to say that true exactly-once delivery is impossible over an unreliable network. The real answer is at-least-once plus idempotency: dedup stores, the outbox pattern, and Kafka''s transactional exactly-once (and what it doesn''t cover).'
reading_time: 6
tags:
- messaging
- delivery-semantics
- idempotency
- exactly-once
- kafka
- outbox
- transactions
sources:
- title: Confluent Docs — Message Delivery Guarantees for Apache Kafka
  url: https://docs.confluent.io/kafka/design/delivery-semantics.html
- title: 'Confluent Blog — Exactly-Once Semantics Are Possible: Here''s How Apache Kafka Does It'
  url: https://www.confluent.io/blog/exactly-once-semantics-are-possible-heres-how-apache-kafka-does-it/
- title: KIP-98 — Exactly Once Delivery and Transactional Messaging
  url: https://cwiki.apache.org/confluence/display/KAFKA/KIP-98+-+Exactly+Once+Delivery+and+Transactional+Messaging
- title: Brave New Geek — You Cannot Have Exactly-Once Delivery
  url: https://bravenewgeek.com/you-cannot-have-exactly-once-delivery/
- title: Strimzi Blog — Exactly-once semantics with Kafka transactions
  url: https://strimzi.io/blog/2023/05/03/kafka-transactions/
- title: KIP-679 — Producer will enable the strongest delivery guarantee by default
  url: https://cwiki.apache.org/confluence/display/KAFKA/KIP-679:+Producer+will+enable+the+strongest+delivery+guarantee+by+default
- title: Message Delivery Semantics (Apache Kafka documentation)
  url: https://kafka.apache.org/documentation/#semantics
---

"What delivery guarantee do you want?" is one of those interview questions where the correct first move is to refuse the premise. There are three named guarantees, and only two of them are real. Getting this right — and knowing exactly why the third one is a marketing label rather than a network property — is the difference between a rehearsed answer and one that shows you understand failure.

## The three guarantees, defined

A producer sends a message; a broker stores it; a consumer processes it. Between each hop is a network that can drop, delay, duplicate, or reorder, and a process that can crash at the worst possible instant. The delivery semantic describes what survives that.

| Semantic | Guarantee | Failure mode | You get it by |
|---|---|---|---|
| **At-most-once** | 0 or 1 deliveries | Messages can be **lost** | Ack/commit *before* processing (fire-and-forget) |
| **At-least-once** | 1 or more deliveries | Messages can be **duplicated** | Ack/commit *after* processing |
| **Exactly-once** | Effectively 1 | Neither loss nor visible duplication | At-least-once **+** dedup/idempotency, or an atomic commit |

The whole thing turns on **when you acknowledge**. That is the only lever you actually control.

## Why the acknowledgement timing decides everything

Consider a consumer pulling from a queue. It must do two things: process the message (write to a DB, call an API) and acknowledge it (commit the offset, delete from the queue). These are two separate operations on two separate systems, and a crash can land between them.

- **Ack first, then process** → if you crash after the ack but before processing finishes, the message is gone. Nobody will redeliver it, because as far as the broker knows, you're done. That's **at-most-once**: cheap, low-latency, lossy.
- **Process first, then ack** → if you crash after processing but before the ack, the broker never heard "done," so on restart it redelivers. You process the same message twice. That's **at-least-once**: no loss, but duplicates.

There is no third ordering. You either risk losing the message or risk seeing it twice — you cannot have neither purely by ordering two operations across a boundary you don't control. This is why **at-least-once is the practical default**: silent data loss is usually catastrophic, while a duplicate is a problem you can engineer around. So you choose duplicates and do the engineering.

## Why "exactly-once delivery" is impossible

The reason isn't implementation laziness — it's a genuine impossibility result. Picture the last message hop. The consumer receives message `m` and sends back an ack. Two things can go wrong with that ack: it can be lost, or it can be delayed past the sender's timeout. From the **sender's** side, a missing ack is indistinguishable between "consumer never got `m`" and "consumer got `m` and processed it, but the ack vanished."

The sender now has exactly two choices, and both are wrong in one of the cases:

- **Retransmit** `m` → correct if it was lost, a **duplicate** if the ack was the thing that was lost.
- **Don't retransmit** → correct if it was delivered, a **lost message** if it wasn't.

This is the **Two Generals' Problem**: no finite exchange of messages over a lossy channel lets both sides *agree* that delivery happened. Add the possibility of process crashes and you're in **FLP** territory too. As the Brave New Geek write-up puts it bluntly, "there is no such thing as exactly-once delivery" — any system that advertises it is really giving you at-least-once plus deduplication, or it is, in the author's words, "lying to your face."

So the honest reframing, and the one interviewers are listening for: you cannot guarantee exactly-once **delivery**, but you *can* guarantee exactly-once **processing** — the message may arrive many times, but its *effect* lands exactly once.

## Mechanism 1: idempotent consumers (dedup by message ID)

The most general technique. Give every message a stable unique ID at the producer, and have the consumer record which IDs it has already applied. Processing becomes: "if I've seen this ID, skip; otherwise apply the effect and record the ID **atomically**." The atomicity is the crux — the dedup insert and the business write must commit together, or a crash between them reopens the duplicate window.

```python
def handle(msg, conn):
    # msg.id is a producer-assigned unique key (e.g. a UUID)
    with conn:  # single DB transaction = atomic dedup + effect
        cur = conn.execute(
            "INSERT INTO processed_messages (msg_id) VALUES (?) "
            "ON CONFLICT (msg_id) DO NOTHING",
            (msg.id,),
        )
        if cur.rowcount == 0:
            return  # already applied — this is a redelivery, drop it

        # first time we've seen this id: apply the real effect
        conn.execute(
            "UPDATE accounts SET balance = balance + ? WHERE id = ?",
            (msg.amount, msg.account_id),
        )
    # ack happens only after the transaction commits
```

Because the `INSERT` and the `UPDATE` share one transaction, a redelivery either finds the ID already present (and no-ops) or replays the whole unit cleanly. The `processed_messages` table is your **dedup store**; give it a TTL sized to your maximum redelivery window so it doesn't grow forever. A message that can never be processed even after retries should be routed to a [dead-letter queue](/articles/microservices/2026-07-30-dead-letter-queues-poison-messages) rather than looped forever. This is the same machinery as client-facing [idempotency keys](/articles/microservices/2026-07-30-idempotency-keys-safe-retries), applied to the message boundary instead of the API boundary.

Where the operation is *naturally* idempotent — `SET balance = 100`, `PUT` of a full object, "mark as shipped" — you may not even need a dedup store, because applying it twice is indistinguishable from applying it once. Prefer that when you can design for it.

## Mechanism 2: the outbox pattern for atomic DB + publish

Idempotent consumers fix the *receiving* side. But the producer has a symmetric problem: it wants to update its database **and** publish an event, and those are two systems again. If it writes the DB then crashes before publishing, downstream never hears about the change; publish-first has the mirror failure. This is the **dual-write problem**.

The **transactional outbox** collapses the two writes into one. Inside the same DB transaction as the business change, insert a row into an `outbox` table. A separate relay process (often reading the DB's change log via CDC) reads the outbox and publishes to the broker, marking rows sent. Because the business write and the outbox insert are one atomic commit, you never publish an event for a change that didn't persist, and you never persist a change whose event is lost — the relay retries until the broker acks. Note the relay itself is **at-least-once** (it can crash after publishing, before marking sent), which is exactly why the consumer still needs dedup. The two mechanisms compose. Full treatment in the [transactional outbox pattern](/articles/microservices/2026-07-26-transactional-outbox-pattern) article.

## Mechanism 3: Kafka's exactly-once semantics

Kafka is the poster child for "exactly-once," and it's worth stating precisely what it does — and doesn't — deliver. Introduced in 0.11 via KIP-98, it has two building blocks:

- **Idempotent producer.** Each producer gets a **producer ID (PID)**, and every message batch carries a monotonic **sequence number** per partition. The broker tracks the last sequence it accepted and discards a batch it has already seen. So a producer retry after a lost ack — the classic duplicate source — is silently deduplicated *inside the log*, not just in memory. This gives exactly-once **per partition**, surviving leader failover because the sequence lives in the replicated log.
- **Transactions.** A transactional producer writes to multiple partitions such that "either all messages in the batch are eventually visible to any consumer or none are ever visible." Crucially, the consumer's **offset commit can be included in the same transaction** as the output records. Consumers set `isolation.level=read_committed` to hide aborted/uncommitted data.

Together these make the **read-process-write** loop atomic: consume from topic A, transform, produce to topic B, and commit the input offset — all-or-nothing. Kafka Streams wraps this behind a single `processing.guarantee=exactly_once_v2`.

Here's the myth-busting part interviewers reward. This guarantee holds **only within Kafka's own boundary**. The Confluent docs are explicit that exactly-once "is guaranteed within the scope of Kafka Streams' internal processing only; ... if the app makes an RPC call to update some remote store, or uses a customized client to directly read or write a topic, the resulting side effects would not be guaranteed exactly once." Send an email, charge a card, or write to an external DB inside your processor, and you are back to two-generals — that external effect needs its own idempotency key. Kafka gives you exactly-once *within the log*; it does **not** give you end-to-end exactly-once *side effects*.

## The one-sentence answer

Assume at-least-once delivery, because that's the strongest thing an unreliable network can actually give you; then make the *effect* exactly-once with idempotency and deduplication — a dedup store keyed by message ID, an outbox to close the dual-write gap on the way out, and, if you're inside Kafka, transactions to make read-process-write atomic. "Exactly-once" is a property you build in the application, not one you buy from the wire.

**Try next:** add a message-ID dedup table to one of your at-least-once consumers, then deliberately redeliver the same message twice and confirm the business effect lands only once — and trace what happens to an *external* side effect in that same handler.
