---
title: 'Delivery semantics: at-most-once, at-least-once, and the ''exactly-once'' myth'
date: 2026-08-10
track: distributed-systems
summary: 'Three delivery guarantees, defined precisely, and why true exactly-once delivery is unattainable over an unreliable network. The workable construction is at-least-once plus idempotency: dedup stores, the outbox pattern, and Kafka''s transactional exactly-once — with its documented boundary.'
reading_time: 7
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

**Gist.** A message crossing a lossy channel between two processes that can crash admits only two honest guarantees: it may be lost, or it may be duplicated. The practical construction chooses duplication — acknowledge after processing — and then suppresses the duplicate's *effect* with a deduplication record committed atomically alongside the business write. The cost is a persistent dedup store with a retention window, an extra write on every message, and the requirement that every external side effect carry its own idempotency key.

## The three guarantees, defined

A producer sends a message, a broker stores it, a consumer processes it. Between each hop sits a network that can drop, delay, duplicate or reorder, and a process that can crash between any two instructions. The delivery semantic names what survives that.

| Semantic | Guarantee | Failure mode | Obtained by |
|---|---|---|---|
| **At-most-once** | 0 or 1 deliveries | Messages can be **lost** | Ack/commit *before* processing (fire-and-forget) |
| **At-least-once** | 1 or more deliveries | Messages can be **duplicated** | Ack/commit *after* processing |
| **Exactly-once** | Effectively 1 | Neither loss nor visible duplication | At-least-once **+** dedup/idempotency, or an atomic commit |

The distinction reduces to **the ordering of the acknowledgement relative to the effect**. That ordering is the only lever the consumer controls.

## Why acknowledgement timing decides the semantic

A consumer performs two operations on two different systems: it applies the effect (a database write, a remote call) and it acknowledges (commits the offset, deletes from the queue). A crash can land between them, and the two orderings exhaust the design space.

- **Ack, then process.** A crash after the acknowledgement and before the effect completes destroys the message. The broker considers the delivery complete and never redelivers. This is **at-most-once**: no duplicates, no redelivery, and silent loss.
- **Process, then ack.** A crash after the effect and before the acknowledgement leaves the broker with no record of completion, so it redelivers on restart and the effect is applied twice. This is **at-least-once**: no loss, duplicates possible.

No third ordering exists, because the acknowledgement and the effect are not a single atomic action across the process boundary. **At-least-once is the common default**: loss is generally unrecoverable, whereas a duplicate is a condition the application can be built to absorb.

## Why exactly-once *delivery* is unattainable

Consider the final hop. The consumer receives message `m` and returns an acknowledgement. That acknowledgement can be lost, or delayed past the sender's timeout. From the **sender's** vantage point the two states — "`m` never arrived" and "`m` arrived and was processed, but the acknowledgement did not return" — are **indistinguishable**, because the only evidence distinguishing them is the message that failed to arrive.

The sender therefore has two moves, each wrong in one of the two states:

- **Retransmit** `m`: correct if `m` was lost, a **duplicate** if the acknowledgement was lost.
- **Do not retransmit**: correct if `m` arrived, a **lost message** if it did not.

This is the **Two Generals' Problem**: no finite exchange of messages over a lossy channel establishes common knowledge that delivery occurred. A process that can also crash removes the remaining escape route, since no amount of retransmission distinguishes a peer that is slow from one that is gone. The Brave New Geek write-up states the conclusion directly — "there is no such thing as exactly-once delivery" — and characterises systems advertising it as offering at-least-once plus deduplication.

The usable reframing separates two properties: exactly-once **delivery** is unattainable, while exactly-once **processing** is attainable. The message may arrive an unbounded number of times; its *effect* is applied once.

## Mechanism 1: idempotent consumers keyed by message identifier

The general technique assigns every message a **stable unique identifier at the producer** and has the consumer record the identifiers it has applied. Processing becomes conditional: if the identifier is present, skip; otherwise apply the effect and record the identifier **in the same transaction**.

The atomicity is load-bearing. If the dedup insert and the business write commit separately, a crash between them leaves a state in which the effect is applied but unrecorded (the redelivery applies it again) or recorded but unapplied (the redelivery suppresses an effect that never happened). **One transaction collapses both windows**: a redelivery either observes the identifier and no-ops, or replays the entire unit.

The `processed_messages` table is the **dedup store**. It requires a retention policy sized to the **maximum redelivery window**, because entries older than any possible redelivery cannot suppress anything and unbounded growth is otherwise the outcome. A message that still fails after its retry budget belongs in a [dead-letter queue](/articles/microservices/2026-07-30-dead-letter-queues-poison-messages) rather than an unbounded redelivery loop. The machinery is that of client-facing [idempotency keys](/articles/microservices/2026-07-30-idempotency-keys-safe-retries), applied at the message boundary instead of the API boundary.

Where the operation is **naturally idempotent** — assigning an absolute balance, a `PUT` of a complete object, a transition to a terminal state such as "shipped" — no dedup store is required, because a second application is indistinguishable in its result from the first.

### Implementation sketch (Scala)

```scala
final case class Message(id: String, accountId: Long, amount: Long)

/** Applies `msg` at most once against `conn`; the dedup row and the business
  * write share one transaction, so a crash cannot separate them. */
def handle(conn: java.sql.Connection, msg: Message): Unit =
  conn.setAutoCommit(false)
  try
    val claim = conn.prepareStatement(
      "INSERT INTO processed_messages (msg_id) VALUES (?) ON CONFLICT (msg_id) DO NOTHING")
    claim.setString(1, msg.id)
    val firstSighting = claim.executeUpdate() == 1

    if firstSighting then
      val apply = conn.prepareStatement(
        "UPDATE accounts SET balance = balance + ? WHERE id = ?")
      apply.setLong(1, msg.amount)
      apply.setLong(2, msg.accountId)
      apply.executeUpdate()

    conn.commit()          // acknowledgement to the broker happens only after this returns
  catch
    case e: Throwable =>
      conn.rollback()      // no dedup row survives, so redelivery replays the whole unit
      throw e
```

A zero row count from the `INSERT` identifies a redelivery, and the handler returns without a second effect. Rollback is what preserves the invariant: **the dedup row exists if and only if the effect is committed**.

## Mechanism 2: the transactional outbox

Idempotent consumers address the receiving side. The producer faces the symmetric **dual-write problem**: it must update its database *and* publish an event, again across two systems. Writing the database and then crashing before publishing leaves downstream unaware of a committed change; publishing first has the mirrored failure, an event describing a change that never persisted.

The **transactional outbox** reduces the two writes to one. Inside the transaction carrying the business change, a row is inserted into an `outbox` table. A separate relay process — often reading the database's change log via change data capture (CDC) — reads the outbox, publishes to the broker, and marks rows sent. Because the business write and the outbox insert share a commit, **no event is published for a change that did not persist, and no persisted change loses its event**, since the relay retries until the broker acknowledges.

The relay is itself **at-least-once**: it can crash after publishing and before marking the row sent, republishing on restart. This is the reason the consumer-side dedup store remains necessary; the two mechanisms compose rather than substitute. The [transactional outbox pattern](/articles/microservices/2026-07-26-transactional-outbox-pattern) article treats the relay in full.

## Mechanism 3: Kafka's exactly-once semantics and their boundary

Kafka introduced exactly-once semantics in 0.11 via KIP-98, on two building blocks.

- **Idempotent producer.** Each producer receives a **producer ID (PID)**, and every message batch carries a **monotonic sequence number per partition**. The broker records the last sequence it accepted and discards a batch it has already seen. A producer retry following a lost acknowledgement — the canonical duplicate source — is therefore deduplicated **inside the log**. The broker's per-producer sequence state is persisted with the partition rather than held only in the leader's memory.
- **Transactions.** A transactional producer writes to multiple partitions such that, in the KIP's terms, either all messages in the transaction are eventually visible to any consumer or none are ever visible. The **consumer's offset commit can be enlisted in the same transaction** as the output records. Consumers set `isolation.level=read_committed` to hide aborted and uncommitted data.

Together these make the **read-process-write** loop atomic: consume from topic A, transform, produce to topic B, commit the input offset, all-or-nothing. Kafka Streams exposes this as `processing.guarantee=exactly_once_v2`.

The boundary is documented rather than implied. The Confluent documentation states that the guarantee "is guaranteed within the scope of Kafka Streams' internal processing only; ... if the app makes an RPC call to update some remote store, or uses a customized client to directly read or write a topic, the resulting side effects would not be guaranteed exactly once." An email dispatch, a card charge, or a write to an external database inside a processor returns the system to the two-generals situation, and each such effect requires its own idempotency key. Kafka provides exactly-once **within the log**; it does not provide end-to-end exactly-once **side effects**.

## Pitfalls

- **Dedup insert and business write in separate transactions.** Symptom: duplicates appear at a low rate under crash-restart, and only under crash-restart. Cause: the crash window between the two commits leaves the effect applied but unrecorded.
- **Dedup store with no retention bound.** Symptom: the table grows without limit and its index degrades over time. Cause: entries are never expired against the maximum redelivery window.
- **Consumer-assigned message identifiers.** Symptom: a producer retry after a lost acknowledgement is treated as a new message and applied twice. Cause: the identifier must be stable across retransmissions, which requires assignment at the producer.
- **Assuming the outbox relay publishes once.** Symptom: downstream sees repeated events for a single business change. Cause: the relay can crash between publishing and marking the row sent.
- **External side effects inside a Kafka transaction.** Symptom: a duplicate charge or duplicate email despite `exactly_once_v2`. Cause: the documented guarantee covers Kafka-internal processing, not remote calls.
- **Consumers left at the default isolation level.** Symptom: records from an aborted transaction are observed downstream. Cause: `read_committed` is required to filter aborted and uncommitted data.
- **Acknowledging before the transaction commits.** Symptom: silent loss under crash. Cause: the ordering has been converted to at-most-once.
