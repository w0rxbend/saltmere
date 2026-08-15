---
title: "Kafka Share Groups: Queue Semantics under KIP-932"
date: 2026-07-31
track: microservices
summary: "How KIP-932 share groups add per-record acknowledgement and partition-independent parallelism to Kafka — early access in 4.0, preview in 4.1, production-ready in 4.2.0 (Feb 2026)."
reading_time: 7
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

**Gist.** A classic Kafka consumer group binds each partition to exactly one member, so consumption parallelism is capped by the partition count and a single slow or failing record blocks the offset behind it. KIP-932 introduces the **share group**, in which many consumers read the same partitions cooperatively and acknowledge **individual records** under a broker-held acquisition lock. The cost is per-partition ordering: records are handed to whichever member is free, and the broker must now track per-record state rather than one monotonic offset per partition.

Share groups shipped as **Early Access in Kafka 4.0** (March 2025), moved to **Preview in 4.1.0** (September 2025), and became **production-ready in 4.2.0** (17 February 2026).

## Share group compared with consumer group

In a consumer group, partition assignment is exclusive. A topic with 12 partitions supports at most 12 usefully occupied members of one group; a thirteenth member idles. In exchange, records within a partition are delivered in offset order, and group progress is a single committed offset per partition that advances monotonically. Recovery is cheap because that offset is the whole of the consumer-side state.

A share group removes the exclusivity. **A partition may be handed to many members of the same share group simultaneously, and the number of consumers is not bounded by the number of partitions.** Throughput scales by adding consumer processes rather than by repartitioning the topic — repartitioning being the operation that, in the consumer-group model, changes key-to-partition mapping and therefore disturbs ordering guarantees for keyed data. What is surrendered is per-partition ordering: two records from the same partition can be in flight at two different members at the same time, and nothing sequences their completion.

## Per-record acknowledgement and the record state machine

A consumer group commits a position, which is an assertion about a prefix: everything below this offset is done. A share group instead tracks each record through the states

    Available → Acquired → Acknowledged → Archived

and the consumer supplies one of three acknowledgement types per record:

- **ACCEPT** — processing succeeded; the record is complete for this group.
- **RELEASE** — a transient failure; the record returns to `Available` and becomes eligible for redelivery, to this member or another.
- **REJECT** — the record is permanently unprocessable; it is not delivered again.

The consequence is that **one failing record no longer holds back the records behind it**, because there is no shared prefix position that its failure would pin. That property, not the fan-out, is the semantic that consumer groups could not express: with a committed offset, skipping a bad record means either committing past it (losing it) or stalling.

## The acquisition lock

The mechanism enabling several members to read one partition without duplicating work is the **acquisition lock**, held on the broker by the share-partition leader (the `SharePartitionManager` subsystem). When a member fetches a record, the record enters `Acquired` and is **invisible to every other member of the same share group** for the duration configured by `group.share.record.lock.duration.ms`, whose default is 30 seconds.

Two outcomes are possible. If the member acknowledges within the lock window, the record settles into `Acknowledged` — or back to `Available` on RELEASE. If the lock expires without acknowledgement, which is what a crashed, partitioned or stalled consumer produces, **the record reverts to `Available` and is offered to another member**. Liveness therefore rests on a broker-side timeout rather than on group membership: recovery does not require detecting that a consumer died, nor reassigning partition ownership, because ownership was never granted.

The lock is a lease, not a mutex the client holds, so the usual lease hazard applies: a consumer that exceeds the lock duration but is still alive can finish processing a record that the broker has already handed to someone else. Delivery is **at-least-once**, and processing must be idempotent.

## Delivery counts and poison messages

Every acquisition increments a **delivery count** for that record. Together with `group.share.delivery.count.limit`, default **5**, this bounds redelivery: a record that is repeatedly released or repeatedly left to expire is **archived once the limit is reached** rather than circulating indefinitely. Note that lock expiry counts the same as an explicit RELEASE for this purpose — a consumer that is merely slow consumes delivery attempts. A native dead-letter queue is listed as future work in KIP-932 rather than shipped; an archived record is not routed anywhere, so any record the application must keep has to be captured before REJECT or before the limit is exhausted.

## Enabling and using share groups

Share groups are gated by the KRaft feature-flag system, so the feature is turned on cluster-wide:

```bash
bin/kafka-features.sh --bootstrap-server localhost:9092 \
  upgrade --feature share.version=1
```

The share rebalance protocol must also be enabled on the brokers:

```properties
group.coordinator.rebalance.protocols=classic,consumer,share
```

The client entry point is `KafkaShareConsumer`. There is no partition assignment to observe and no offset arithmetic; records are acknowledged directly:

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
    consumer.commitSync(); // flushes acknowledgements to the share coordinator
}
```

Group-level settings such as the initial position live on the group configuration resource (`ConfigResource.Type.GROUP`) — for example `share.auto.offset.reset=earliest`, applied through `incrementalAlterConfigs` or `kafka-configs.sh`.

### Implementation sketch (Scala)

The following models the broker-side transition rules described above — acquisition, the three acknowledgement types, lock expiry and the delivery count limit — for a single record. It is a state machine, not a share-partition implementation: replication, fetch batching and the share coordinator are all absent.

```scala
enum State:
  case Available, Acquired, Acknowledged, Archived

enum Ack:
  case Accept, Release, Reject

final case class Record(
    offset: Long,
    state: State,
    deliveryCount: Int,
    lockExpiresAt: Long // valid only while Acquired
)

final class SharePartition(lockDurationMs: Long, deliveryCountLimit: Int):

  def acquire(r: Record, now: Long): Option[Record] =
    if r.state != State.Available then None
    else if r.deliveryCount >= deliveryCountLimit then
      Some(r.copy(state = State.Archived)) // limit reached before hand-out
    else
      Some(r.copy(
        state = State.Acquired,
        deliveryCount = r.deliveryCount + 1,
        lockExpiresAt = now + lockDurationMs
      ))

  /** Acknowledgement is ignored once the lock has lapsed: the record may
    * already be Acquired by another member. */
  def acknowledge(r: Record, ack: Ack, now: Long): Record =
    if r.state != State.Acquired || now >= r.lockExpiresAt then r
    else ack match
      case Ack.Accept  => r.copy(state = State.Acknowledged)
      case Ack.Reject  => r.copy(state = State.Archived)
      case Ack.Release => release(r)

  /** Expiry is indistinguishable from RELEASE for the delivery count. */
  def expireLock(r: Record, now: Long): Record =
    if r.state == State.Acquired && now >= r.lockExpiresAt then release(r) else r

  private def release(r: Record): Record =
    if r.deliveryCount >= deliveryCountLimit then r.copy(state = State.Archived)
    else r.copy(state = State.Available)
```

## Choosing between the two models

Consumer groups remain the model for ordered, replayable, partition-aligned processing, where the committed offset is both the progress marker and the replay handle. Share groups apply where work is independent and parallelism should not be capped by partition count. The exchange is stated plainly by the mechanism: per-record state and a broker-held lease buy fan-out and individual failure handling, and they cost per-partition ordering plus the broker memory and bookkeeping that per-record state requires.

## Pitfalls

- **Processing that outruns `group.share.record.lock.duration.ms` (default 30 s) produces duplicate work.** The lock is a lease; on expiry the broker returns the record to `Available` and another member may acquire it while the first is still running.
- **A slow consumer exhausts the delivery count limit without any explicit failure.** Lock expiry increments the delivery count exactly as RELEASE does, so five successive timeouts archive the record under the default `group.share.delivery.count.limit` of 5.
- **An archived record is not delivered anywhere.** No native dead-letter queue exists yet, so records lost to REJECT or to the delivery count limit are unrecoverable unless the application persists them first.
- **Expecting per-partition order in a share group.** Multiple members hold records from the same partition concurrently; nothing orders their completion, so keyed sequences must not depend on it.
- **Enabling only the feature flag, or only the rebalance protocol.** Both `share.version=1` and `share` in `group.coordinator.rebalance.protocols` are required; setting one alone leaves share consumers unable to form a group.
- **Reaching for `ConfigResource.Type.TOPIC` for share settings.** Group-scoped options such as `share.auto.offset.reset` live on `ConfigResource.Type.GROUP` and are silently unrelated to topic configuration.
