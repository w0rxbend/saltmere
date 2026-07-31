---
title: "Kafka Grows a Queue: Share Groups (KIP-932)"
date: 2026-07-31
track: microservices
summary: "How KIP-932 share groups add per-record ack and partition-independent parallelism to Kafka — early access in 4.0, preview in 4.1, production-ready in 4.2.0 (Feb 2026)."
reading_time: 5
tags: [kafka, share-groups, kip-932, queues, consumers, messaging]
sources:
  - title: "KIP-932: Queues for Kafka (Apache wiki)"
    url: "https://cwiki.apache.org/confluence/display/KAFKA/KIP-932%3A+Queues+for+Kafka"
  - title: "Apache Kafka 4.2.0 Release Announcement"
    url: "https://kafka.apache.org/blog/2026/02/17/apache-kafka-4.2.0-release-announcement/"
  - title: "Apache Kafka 4.1.0 Release Announcement"
    url: "https://kafka.apache.org/blog/2025/09/04/apache-kafka-4.1.0-release-announcement/"
  - title: "Kafka Queue Semantics Now GA with Share Consumer API (Confluent)"
    url: "https://www.confluent.io/blog/kafka-queue-semantics-share-consumer-ga/"
  - title: "Let's Take a Look at KIP-932: Queues for Kafka (Gunnar Morling)"
    url: "https://www.morling.dev/blog/kip-932-queues-for-kafka/"
---

For fifteen years Kafka gave you exactly one consumption model: a partitioned log with consumer groups, where each partition is owned by exactly one consumer in the group. That's great for ordered, replayable streams — and awful when you just want a work queue. KIP-932 finally adds queue semantics as a first-class feature. It landed as **Early Access in Kafka 4.0** (March 2025), moved to **Preview in 4.1.0** (September 2025), and became **production-ready in 4.2.0** (February 17, 2026).

## Share group vs consumer group

A classic consumer group binds a partition to a single member. Your maximum parallelism is the partition count: 12 partitions means at most 12 useful consumers, and adding a 13th just gives you an idle process. Ordering is preserved per partition, and offsets advance monotonically.

A **share group** inverts that. Multiple consumers cooperatively read the *same* partitions. A partition can be handed to many members, and — the headline change — the number of consumers can exceed the number of partitions. You scale throughput by adding consumers, not by repartitioning the topic. In exchange you give up strict per-partition ordering: records are distributed to whoever is free, exactly like a traditional message queue.

## Per-record acknowledgement

Consumer groups commit a single offset — "I've processed everything up to here." Share groups acknowledge **individual records**, and each record moves through states: `Available → Acquired → Acknowledged → Archived`. There are three acknowledgement types:

- **ACCEPT** — processed successfully; the record is done.
- **RELEASE** — transient failure; return it to `Available` for redelivery to someone else.
- **REJECT** — permanently unprocessable; don't deliver again.

This is the semantic Kafka never had: you can fail one message without blocking the ones behind it.

## The acquisition lock and delivery counts

The mechanism behind cooperative reads is the **acquisition lock**, managed on the broker by the share-partition leader (the `SharePartitionManager` subsystem). When a consumer fetches a record it enters `Acquired` state and is invisible to other members for `share.record.lock.duration.ms` (default 30s). Acknowledge it and it settles; let the lock expire without acknowledging — because you crashed or stalled — and it reverts to `Available` for someone else. That timeout-based release is what makes delivery robust without partition ownership.

Every acquisition bumps a **delivery count**. Combined with `group.share.delivery.attempt.limit` (default **5**), this gives at-least-once delivery with automatic poison-message handling: a record that keeps failing gets redelivered up to the limit, then is archived rather than looping forever. A native DLQ is on the roadmap.

## Enabling and using it

Share groups ride on the KRaft feature-flag system. Turn the feature on cluster-wide:

```bash
bin/kafka-features.sh --bootstrap-server localhost:9092 \
  upgrade --feature share.version=1
```

Make sure the share rebalance protocol is enabled on brokers:

```properties
group.coordinator.rebalance.protocols=classic,consumer,share
```

Then use the new `KafkaShareConsumer`. Note there's no `subscribe`-to-partition dance and no offset arithmetic — you acknowledge records directly:

```java
Properties props = new Properties();
props.setProperty("bootstrap.servers", "localhost:9092");
props.setProperty("group.id", "my-share-group");

KafkaShareConsumer<String, String> consumer =
    new KafkaShareConsumer<>(props,
        new StringDeserializer(), new StringDeserializer());

consumer.subscribe(List.of("orders"));

while (true) {
    ConsumerRecords<String, String> records =
        consumer.poll(Duration.ofMillis(100));
    for (ConsumerRecord<String, String> record : records) {
        if (isProcessable(record)) {
            process(record);
            consumer.acknowledge(record, AcknowledgeType.ACCEPT);
        } else if (isRetriable(record)) {
            consumer.acknowledge(record, AcknowledgeType.RELEASE);
        } else {
            consumer.acknowledge(record, AcknowledgeType.REJECT);
        }
    }
    consumer.commitSync(); // flushes acks to the share coordinator
}
```

Group-level knobs like the initial position live on the group config resource (`ConfigResource.Type.GROUP`), e.g. `share.auto.offset.reset=earliest`, set via `incrementalAlterConfigs` or `kafka-configs.sh`.

The tradeoff is explicit: choose consumer groups when you need ordered, replayable, partition-aligned processing; choose share groups when you need queue-style fan-out where parallelism shouldn't be capped by partition count.

**Try next:** Spin up Kafka 4.2.0, enable `share.version=1`, create a 3-partition topic, and run *five* `KafkaShareConsumer` instances against it. Have one consumer `RELEASE` every record and watch the delivery count climb to `group.share.delivery.attempt.limit` before the record is archived.
