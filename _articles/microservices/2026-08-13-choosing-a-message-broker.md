---
title: "Choosing a message broker: Kafka vs RabbitMQ vs NATS JetStream vs SQS/SNS"
date: 2026-08-13
track: microservices
summary: "The interview version of broker selection: log vs queue semantics, smart-broker vs smart-consumer, and how ordering, fan-out, replay, delayed delivery, and ops cost actually differ across Kafka, RabbitMQ, NATS JetStream, and SQS/SNS. One decision table and a pick-X-when list."
reading_time: 5
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

"Which broker would you pick?" is not a trivia question. The interviewer wants to see that you know the *axes* — and that you ask about the workload before naming a product. Here is the compressed map.

## Two philosophies, two data models

**Smart broker, dumb consumer.** RabbitMQ's model: the broker owns routing (exchanges, bindings), per-message acknowledgement, redelivery, TTLs, and dead-lettering. Consumers are thin; a message is *removed* once acked. This is **queue semantics**: destructive consumption, per-message state, work distribution.

**Dumb broker, smart consumer.** Kafka's model: the broker is an append-only partitioned **log**. It doesn't track "was message X processed" — each consumer group just advances an offset. Delivery state lives client-side, which is what buys sequential-I/O throughput and cheap fan-out: ten groups reading the same log cost roughly one write. This is **log semantics**: non-destructive reads, replay for free, coarse (offset) rather than per-message acknowledgement.

The line is blurring — [Kafka share groups (KIP-932)](/articles/microservices/2026-07-31-kafka-share-groups-queues/) bolt queue semantics onto the log (GA in Kafka 4.2), and RabbitMQ has grown *streams* with non-destructive replay — but "log vs queue" is still the first question to answer out loud.

NATS JetStream sits in between: a lightweight log (streams) with consumer-side cursors *and* broker-tracked per-message acks on durable consumers. SQS is a managed queue with per-message visibility timeouts; SNS is the fan-out layer you bolt on top.

## Decision table

| Axis | Kafka | RabbitMQ (quorum) | NATS JetStream | SQS + SNS |
|---|---|---|---|---|
| Model | Partitioned log | Queue (broker-tracked acks) | Log w/ server-tracked consumers | Managed queue + pub/sub |
| Ordering | Per partition (by key) | Per queue; redelivery can reorder | Per subject within a stream | FIFO: per message group; Standard: best-effort |
| Fan-out | Cheap: N consumer groups, one log | Exchanges copy msg per queue | Multiple consumers per stream | SNS → many SQS queues |
| Replay | Yes — rewind offsets, log is retained | Only via streams, not queues | Yes — replay by seq/time | No — consumption is destructive |
| Delayed delivery | None built in | Per-msg TTL + DLX, or plugin | Native since 2.12 (scheduled msgs) | Native, max 15 min delay |
| Throughput | Very high (GB/s, horizontal via partitions) | Moderate-high; per-queue limits | High for its footprint | Standard: nearly unlimited; FIFO: 300 msg/s/group, more w/ batching + high-throughput mode |
| Latency | Low ms (tunable via acks/linger) | Low ms | Low ms | Tens of ms, plus polling |
| Delivery guarantee | At-least-once; EOS w/ transactions | At-least-once; per-msg ack | At-least-once; dedup window | At-least-once (FIFO: 5-min dedup) |
| Ops cost | Highest: partitions, rebalances, capacity | Moderate: clusters, queue policies | Low: single Go binary, Raft | Zero servers; pay per request |

Details behind three rows interviewers poke at:

- **Ordering** is never global. Kafka orders *within a partition*, so your key choice is your ordering guarantee; SQS FIFO orders *within a message group*; RabbitMQ orders a queue until a redelivery jumps the line. Say "ordered per key/group, not globally" and you're ahead of most candidates.
- **Replay** is the log's superpower: rebuilding a read model or backfilling a new consumer is a rewind, not a re-publish. On SQS or classic RabbitMQ queues, once acked it's gone — replay means keeping a second copy yourself.
- **Delayed delivery** is queue-native territory: SQS gives you 15 minutes, RabbitMQ TTL+dead-letter tricks, JetStream grew first-class scheduled (even cron-style, in 2.14) messages. Kafka has nothing built in — you build delay wheels or use external schedulers, which is worth admitting.

## Pick X when

- **Kafka** — event backbone for many teams: high-throughput streams, multiple independent consumers, replayable history, stream processing. Accept the operational bill (or pay a cloud vendor). Since 4.x it's KRaft-only, [no ZooKeeper](/articles/microservices/2026-07-25-kafka-4-kraft-no-zookeeper/), which removed one moving part.
- **RabbitMQ** — task/work queues and RPC-ish request routing: per-message acks, priorities, TTLs, [dead-letter queues](/articles/microservices/2026-07-30-dead-letter-queues-poison-messages/), complex routing topologies. Quorum queues (Raft) are the default replicated type in the 4.x line.
- **NATS JetStream** — you want ~80% of Kafka's semantics from a [single 20 MB binary](/articles/microservices/2026-08-04-nats-jetstream-event-broker/): edge/IoT, resource-constrained clusters, teams without a streaming platform team.
- **SQS/SNS** — you're on AWS and want zero broker operations: work queues, retries with visibility timeouts, SNS→SQS fan-out. Accept no replay, 15-min max delay, and FIFO throughput ceilings.

Two closing moves that score points: (1) name the guarantee you actually need — "at-least-once plus [idempotent consumers](/articles/microservices/2026-07-30-idempotency-keys-safe-retries/)" beats chasing [exactly-once](/articles/microservices/2026-08-13-exactly-once-delivery-semantics-kafka/) — and (2) note that hybrid is normal: Kafka as the backbone, SQS for one team's work queue, is not an architectural failure.

**Try next:** take one queue or topic you run today and answer the table's rows for it — required ordering scope, fan-out count, replay need, max tolerable delay. If two or more answers fight your current broker's model, sketch the migration before an incident does it for you.
