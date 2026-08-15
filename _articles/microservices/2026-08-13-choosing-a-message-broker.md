---
title: "Choosing a message broker: Kafka vs RabbitMQ vs NATS JetStream vs SQS/SNS"
date: 2026-08-13
track: microservices
summary: "Broker selection by axis: log vs queue semantics, smart-broker vs smart-consumer, and how ordering, fan-out, replay, delayed delivery, and operational cost differ across Kafka, RabbitMQ, NATS JetStream, and SQS/SNS. One decision table and a selection list."
reading_time: 6
tags: [message-brokers, kafka, rabbitmq, nats-jetstream, sqs-sns]
sources:
  - title: "Apache Kafka documentation"
    url: "https://kafka.apache.org/documentation/"
  - title: "RabbitMQ — Quorum Queues"
    url: "https://www.rabbitmq.com/docs/quorum-queues"
  - title: "NATS Docs — JetStream"
    url: "https://docs.nats.io/nats-concepts/jetstream"
  - title: "AWS — Amazon SQS high throughput for FIFO queues"
    url: "https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/high-throughput-fifo.html"
  - title: "RabbitMQ 4.3 Highlights"
    url: "https://www.rabbitmq.com/blog/2026/04/23/rabbitmq-4.3-release"
---

**Gist.** Broker selection is a choice about **where delivery state lives**: in the broker, as per-message acknowledgement records, or in the client, as a cursor into an append-only log. The queue model buys per-message redelivery, priorities, dead-lettering and delayed delivery at the price of per-message bookkeeping and destructive consumption; the log model buys cheap fan-out and replay at the price of coarse, offset-granular acknowledgement and consumer-side state. Every row of the comparison below — ordering scope, replay, delay, throughput ceiling, operational cost — follows from that one placement decision.

## Two philosophies, two data models

**Smart broker, dumb consumer.** RabbitMQ places delivery state in the broker: exchanges and bindings own routing, and the broker records per-message acknowledgement, redelivery, time-to-live (TTL) expiry and dead-lettering. A message is *removed* once acknowledged. This is **queue semantics**: destructive consumption, per-message state, work distribution across competing consumers.

**Dumb broker, smart consumer.** Kafka places delivery state in the client: the broker is an append-only partitioned **log** and does not record whether a given message was processed. A consumer group advances an **offset**, a single integer per partition. Two consequences follow directly. Writes are sequential appends rather than per-message record mutations, and fan-out is close to free — ten consumer groups reading the same log read the same bytes, so the storage cost is one copy, not ten. This is **log semantics**: non-destructive reads, replay by rewinding the offset, and acknowledgement at offset granularity rather than per message.

The distinction is no longer absolute. [Kafka share groups (KIP-932)](/articles/microservices/2026-07-31-kafka-share-groups-queues/) add queue semantics on top of the log and reached general availability in Kafka 4.2, and RabbitMQ has grown *streams*, which support non-destructive replay. The log-versus-queue question remains the first one to settle, because it determines which of the remaining axes are even available.

NATS JetStream occupies the middle. Streams are a log with sequence numbers, and durable consumers carry **broker-tracked per-message acknowledgement** in addition to a cursor. SQS is a managed queue whose in-flight state is expressed as a **visibility timeout** — a message becomes invisible for a bounded interval after receipt and returns to the queue if not deleted within it. SNS is a separate fan-out layer placed in front of SQS queues.

## Decision table

| Axis | Kafka | RabbitMQ (quorum) | NATS JetStream | SQS + SNS |
|---|---|---|---|---|
| Model | Partitioned log | Queue (broker-tracked acks) | Log w/ server-tracked consumers | Managed queue + pub/sub |
| Ordering | Per partition (by key) | Per queue; redelivery can reorder | Per subject within a stream | FIFO: per message group; Standard: best-effort |
| Fan-out | Cheap: N consumer groups, one log | Exchanges copy msg per queue | Multiple consumers per stream | SNS → many SQS queues |
| Replay | Yes — rewind offsets, log is retained | Only via streams, not queues | Yes — replay by seq/time | No — consumption is destructive |
| Delayed delivery | None built in | Per-msg TTL + DLX, or plugin | No scheduled publish; redelivery deferrable via delayed NAK | Native, max 15 min delay |
| Throughput | Very high; scales horizontally via partitions | Moderate-high; per-queue limits | High for its footprint | Standard: nearly unlimited; FIFO: 300 msg/s per API action, 3,000 with batches of 10, more with high-throughput mode |
| Latency | Low ms (tunable via acks/linger) | Low ms | Low ms | Tens of ms, plus polling |
| Delivery guarantee | At-least-once; EOS w/ transactions | At-least-once; per-msg ack | At-least-once; dedup window | At-least-once (FIFO: 5-min dedup) |
| Ops cost | Highest: partitions, rebalances, capacity | Moderate: clusters, queue policies | Low: single Go binary, Raft | Zero servers; pay per request |

Three rows carry most of the weight.

**Ordering is never global.** Kafka orders records **within a partition**, and the partition is selected by the record key, so **the key choice is the ordering guarantee** — two events that must be ordered relative to each other must share a key. SQS FIFO orders **within a message group**, with the same corollary: the group identifier defines the ordering scope, and it is also the unit across which high-throughput FIFO distributes load. JetStream orders per subject within a stream. RabbitMQ preserves queue order until a redelivery occurs; a message that is negatively acknowledged and requeued re-enters the queue and can be delivered after messages that were originally behind it, so **redelivery breaks order**.

**Replay is a property of retention, not of the protocol.** Rebuilding a read model or introducing a new consumer against a log is a cursor reset over data the broker already holds. Against SQS or a classic RabbitMQ queue, an acknowledged message no longer exists, so replay requires a second copy written by the application at ingest time — a design decision that must be made before it is needed, not after.

**Delayed delivery is queue-native.** SQS supports a delay up to 15 minutes. RabbitMQ expresses delay indirectly, by combining a per-message TTL with a dead-letter exchange (DLX) so expiry routes the message onward. JetStream has no scheduled-publish primitive in the stream model; what it offers is a negative acknowledgement carrying a delay, which defers *redelivery* of an already-published message rather than its first delivery. Kafka has no built-in delay: delay must be constructed outside the broker, with a timer wheel or an external scheduler.

### Implementation sketch (Scala)

The two models differ in the size of the state a consumer must durably record. This sketch contrasts them directly — one integer per partition against one entry per in-flight message.

```scala
type Offset = Long

/** Log model: durable state is a single cursor per partition. */
final case class LogCursor(partition: Int, committed: Offset):
  // A commit at offset n asserts every record < n was processed.
  // Failure after processing but before commit replays [committed, n).
  def commit(upTo: Offset): LogCursor =
    require(upTo >= committed, "offsets advance monotonically")
    copy(committed = upTo)

/** Queue model: durable state is per message, keyed by delivery tag. */
final case class InFlight[A](payload: A, deadline: Long, attempt: Int)

final class QueueState[A](maxAttempts: Int):
  private var inFlight = Map.empty[Long, InFlight[A]]
  private var ready    = Vector.empty[(A, Int)]   // payload with attempts so far
  private var dead     = Vector.empty[A]

  def ack(tag: Long): Unit = inFlight -= tag        // message ceases to exist

  /** Visibility-timeout expiry: unacknowledged work returns to the tail,
    * which is exactly where per-queue ordering is lost. */
  def reclaimExpired(now: Long): Unit =
    val (expired, live) = inFlight.partition(_._2.deadline <= now)
    inFlight = live
    expired.values.foreach: m =>
      if m.attempt + 1 >= maxAttempts then dead :+= m.payload
      else ready :+= (m.payload, m.attempt + 1)
```

The log consumer's recovery point is one number; the queue consumer's is a map whose size grows with concurrency, and whose expiry path is the mechanism that both delivers redelivery and destroys ordering.

## Selection

- **Kafka** — an event backbone shared by many teams: high-throughput streams, multiple independent consumer groups, replayable history, stream processing. The operational cost is partition planning, rebalance behaviour and capacity management, or a vendor invoice. Since the 4.x line Kafka is KRaft-only, with [no ZooKeeper](/articles/microservices/2026-07-25-kafka-4-kraft-no-zookeeper/).
- **RabbitMQ** — task and work queues and request routing: per-message acknowledgement, priorities, TTLs, [dead-letter queues](/articles/microservices/2026-07-30-dead-letter-queues-poison-messages/) and non-trivial routing topologies. Quorum queues, which replicate via Raft, are the default replicated type in the 4.x line.
- **NATS JetStream** — a large fraction of Kafka's semantics from a [single statically linked Go binary](/articles/microservices/2026-08-04-nats-jetstream-event-broker/): edge and IoT deployments, resource-constrained clusters, organisations without a dedicated streaming platform team.
- **SQS/SNS** — an AWS deployment with no appetite for broker operations: work queues, retries driven by visibility timeouts, SNS-to-SQS fan-out. The costs accepted are absence of replay, a 15-minute delay ceiling and the FIFO throughput limit.

Two further observations. First, the guarantee that matters is usually at-least-once delivery combined with [idempotent consumers](/articles/microservices/2026-07-30-idempotency-keys-safe-retries/) rather than [exactly-once](/articles/distributed-systems/2026-08-10-delivery-semantics-exactly-once/) semantics, which constrain the whole pipeline. Second, heterogeneous deployments are ordinary: a Kafka backbone alongside an SQS work queue for one team is a fit-to-workload choice, not an architectural defect.

## Pitfalls

- **Keying for balance rather than for order.** Choosing a Kafka partition key by hash spread produces even load and silently splits a causally ordered entity across partitions; the symptom is out-of-order updates observed under partition-parallel consumption.
- **Assuming a redelivered RabbitMQ message keeps its place.** A negatively acknowledged message is requeued and may arrive after messages that originally followed it, so a consumer relying on queue order will apply updates in the wrong sequence after the first transient failure.
- **Concentrating SQS FIFO traffic in one message group.** High-throughput FIFO raises the quota by spreading load across message groups, so a single group leaves the queue at the default per-API-action rate regardless of how many consumers poll it.
- **Planning to replay from a queue.** Once an SQS or classic RabbitMQ message is acknowledged it is gone, so a backfill request arriving after the fact has no source; the second copy must have been written at ingest.
- **Building delay on Kafka with a sleeping consumer.** Blocking in the poll loop stops progress on the whole partition and, past the configured poll interval, the group rebalances and the work is reassigned rather than delayed.
- **Committing Kafka offsets before processing completes.** A commit asserts that every record below the offset was handled; a crash after the commit drops the in-flight records with no redelivery, converting at-least-once into at-most-once.
- **Ignoring the visibility timeout relative to processing time.** Work that outlives the timeout is redelivered while the first attempt is still running, so a non-idempotent handler executes the same effect twice concurrently.
