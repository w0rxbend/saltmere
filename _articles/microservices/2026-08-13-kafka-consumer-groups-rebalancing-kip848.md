---
title: "Kafka Consumer Groups and Rebalancing: From Stop-the-World to KIP-848"
date: 2026-08-13
track: microservices
summary: "Why classic Kafka rebalances freeze the whole group, how cooperative sticky assignment (KIP-429) and static membership (KIP-345) reduced the pain, and how KIP-848's broker-driven incremental protocol — generally available since Kafka 4.0, opt-in via group.protocol=consumer — replaces the client-side handshake entirely."
reading_time: 6
tags: [kafka, consumer-groups, rebalancing, kip-848, partitions]
sources:
  - title: "KIP-848: The Next Generation of the Consumer Rebalance Protocol"
    url: "https://cwiki.apache.org/confluence/display/KAFKA/KIP-848%3A+The+Next+Generation+of+the+Consumer+Rebalance+Protocol"
  - title: "KIP-848: A New Consumer Rebalance Protocol for Apache Kafka 4.0 (Confluent)"
    url: "https://www.confluent.io/blog/kip-848-consumer-rebalance-protocol/"
  - title: "KIP-429: Kafka Consumer Incremental Rebalance Protocol"
    url: "https://cwiki.apache.org/confluence/display/KAFKA/KIP-429:+Kafka+Consumer+Incremental+Rebalance+Protocol"
  - title: "KIP-345: Introduce static membership protocol to reduce consumer rebalances"
    url: "https://cwiki.apache.org/confluence/display/KAFKA/KIP-345:+Introduce+static+membership+protocol+to+reduce+consumer+rebalances"
  - title: "Rebalance your Kafka partitions with the next generation Consumer Rebalance Protocol (Instaclustr)"
    url: "https://www.instaclustr.com/blog/rebalance-your-apache-kafka-partitions-with-the-next-generation-consumer-rebalance-protocol/"
---

**Gist.** A Kafka consumer group must maintain one invariant — **each subscribed partition is owned by exactly one member at a time** — and every membership or metadata change forces the group to recompute that mapping, an operation called a *rebalance*. The classic protocol enforces the invariant with a global barrier: all members surrender all partitions, one elected member computes the new plan, and everyone resumes, so a single failing consumer stops consumption for the whole group. KIP-848 moves assignment into the broker and reconciles each member independently through its heartbeat, at the cost of a protocol migration and of configuration knobs relocating from client to broker.

## The classic protocol: two phases and one barrier

Every group is served by a **group coordinator**, the broker hosting the group's partition of the internal `__consumer_offsets` topic. In the classic, client-driven protocol a rebalance proceeds in two request rounds:

1. **`JoinGroup`.** Every member sends its subscription. The coordinator collects them, bumps the group's generation, elects one member as **group leader**, and returns the full subscription map to that leader alone.
2. **`SyncGroup`.** The leader runs the assignor selected by `partition.assignment.strategy` — `range` heads the default list, and round-robin, sticky and cooperative-sticky also ship with the client — and uploads the assignment. The coordinator fans the per-member slices back out in the `SyncGroup` responses.

Assignment therefore executes **on a consumer process, not on the broker**, and the coordinator is a relay plus a generation counter. Membership changes that trigger the sequence are: a member joining or leaving, a member missing heartbeats for `session.timeout.ms`, a member exceeding `max.poll.interval.ms` between `poll()` calls (which the client treats as a departure), and subscription or partition-count changes.

## Why the barrier is expensive

The original assignors are **eager**: before the new plan is computed, *every* member revokes *every* partition it holds, including partitions that the new plan returns to the same owner. Consequences follow directly from the barrier:

- **Consumption stops group-wide** for the duration of both rounds, so lag accrues on all partitions rather than only on the moving ones.
- **Records are reprocessed.** A member that cannot commit its offsets before revocation loses the committed position for work already done, and the next owner re-delivers those records. Each eager rebalance is thus a duplicate-delivery event, and **handlers must be idempotent** to survive it.
- **The slowest member sets the pace.** The coordinator waits for the join round to complete, so one member stuck in a long `poll()` handler delays every other member.
- **Stateful consumers pay extra.** Kafka Streams instances flush local state on revocation and may restore state from changelog topics on acquisition, so the pause includes state movement, not only assignment.
- **Failures cascade.** A consumer that flaps — a garbage-collection pause longer than the session timeout, or a crash loop — produces a rebalance on each departure and each rejoin.

The net effect is that eager rebalancing converts one member's fault into a group-wide interruption.

## KIP-429: incremental cooperative rebalancing

`CooperativeStickyAssignor`, available since Kafka 2.4, keeps members on their partitions across the rebalance and revokes **only the partitions whose owner changes**. The revocation is deferred to a **second, short rebalance round**: the first round computes the target and tells members which partitions to give up, the second hands those partitions to their new owners. Partitions that do not move are never paused.

The mechanism removes the stop-the-world property but not the architecture. Assignment still runs on the elected group leader, the whole group still synchronizes on each round, and a second round is now required whenever anything moves.

## KIP-345: static membership

Since Kafka 2.3, giving each consumer instance a stable `group.instance.id` makes its membership **static**. The coordinator binds the assignment to that identifier rather than to the ephemeral member identifier issued at join time, so **a restart that finishes within `session.timeout.ms` reclaims the same identity and the same partitions without any rebalance**. The configuration this implies is a session timeout longer than a planned restart takes. Static membership is the standard treatment for rolling restarts of stateful consumers on Kubernetes, where a StatefulSet ordinal supplies the stable identifier.

## KIP-848: broker-driven reconciliation

KIP-848 became generally available in Kafka 4.0 and removes the client-side handshake:

- **The coordinator computes the assignment.** It tracks subscriptions and topic metadata itself and runs a server-side assignor — `uniform` and `range` ship by default, selected with `group.remote.assignor`. There is no group leader.
- **Reconciliation is per member and has no global barrier.** Each consumer learns what to revoke or acquire in its regular heartbeat response, so **members whose assignment is unchanged never pause**.
- **Timing knobs move to the broker.** `partition.assignment.strategy`, `session.timeout.ms` and `heartbeat.interval.ms` are replaced by group configurations such as `group.consumer.session.timeout.ms`.

The protocol is **opt-in**. A client requests it with `group.protocol=consumer`; `classic` remains the client default, so an existing deployment keeps the old behaviour until it is reconfigured. **Mixed groups are supported** — the coordinator upgrades a classic group when a new-protocol member joins and bridges the remaining classic members — which makes a rolling deployment a valid migration path for a live group. Kafka Streams has its own server-side variant, KIP-1071. Vendor benchmarks report large-group rebalances completing substantially faster; the structural claim independent of any benchmark is that a flapping member now perturbs only the partitions it owned.

```properties
# Kafka >= 4.0 consumer, new rebalance protocol
group.id=orders-enricher
group.protocol=consumer
group.remote.assignor=uniform          # optional, server-side

# Classic-protocol equivalents (pre-4.0 brokers / older clients)
# partition.assignment.strategy=org.apache.kafka.clients.consumer.CooperativeStickyAssignor
# group.instance.id=orders-enricher-0  # static membership (KIP-345)

max.poll.records=500
max.poll.interval.ms=300000
```

### Implementation sketch (Scala)

The client-side contract under cooperative assignment is `ConsumerRebalanceListener`. The callbacks report **deltas, not the full assignment**: `onPartitionsRevoked` receives only the partitions being handed away, and `onPartitionsAssigned` only the newly acquired ones. Offsets for revoked partitions must be committed inside the revocation callback, because the new owner resumes from the last committed position.

```scala
final class StatefulListener(
    consumer: KafkaConsumer[String, String],
    state: mutable.Map[TopicPartition, Long]
) extends ConsumerRebalanceListener:

  def onPartitionsRevoked(revoked: util.Collection[TopicPartition]): Unit =
    // Only the moving partitions arrive here under CooperativeStickyAssignor.
    val offsets = revoked.asScala.iterator
      .flatMap(tp => state.get(tp).map(next => tp -> OffsetAndMetadata(next)))
      .toMap
    if offsets.nonEmpty then consumer.commitSync(offsets.asJava)
    revoked.asScala.foreach(state.remove)

  def onPartitionsAssigned(assigned: util.Collection[TopicPartition]): Unit =
    val committed = consumer.committed(util.HashSet(assigned))
    committed.asScala.foreach:
      case (tp, om) if om != null => state.update(tp, om.offset)
      case _                      => ()

  // Invoked instead of onPartitionsRevoked when the member has already lost
  // ownership (session timeout, poll interval exceeded): committing here fails.
  override def onPartitionsLost(lost: util.Collection[TopicPartition]): Unit =
    lost.asScala.foreach(state.remove)
```

## Choosing partition counts

Partition count bounds the group's parallelism: **an N-partition topic supports at most N actively consuming members per group**, and additional members remain idle. A workable procedure:

1. Divide target throughput by measured per-consumer throughput, then add headroom (2–3×). Headroom matters because **keys are mapped to partitions by hash**, so adding partitions later relocates keys and breaks per-key ordering across the change.
2. Bound the excess. Each partition consumes open file handles and replication fetch traffic, raises end-to-end latency through smaller batches and more leader replication work, and — under the classic protocol — enlarges and slows every rebalance. Dozens of partitions per topic is ordinary; thousands requires justification.
3. Prefer counts with many divisors (12, 24, 30) so that partitions divide evenly across several plausible group sizes.

A workload that requires very large partition counts only because per-message processing is slow is queue-shaped rather than log-shaped. Kafka's share groups (KIP-932) decouple consumer parallelism from partition count and are treated in a separate article.

## Pitfalls

- **A handler slower than `max.poll.interval.ms` triggers a rebalance with no failure present.** The client stops calling `poll()`, the coordinator treats the member as departed, and its partitions move while the member is still processing them.
- **`onPartitionsRevoked` is not invoked when ownership was already lost.** After a session timeout or poll-interval expiry the client calls `onPartitionsLost`; committing offsets there fails, so any offset bookkeeping placed only in the revocation callback is silently skipped.
- **Assuming the callbacks carry the full assignment breaks under cooperative assignment.** With `CooperativeStickyAssignor` the collections hold only the delta, so code that rebuilds all local state from `onPartitionsAssigned` loses the state of retained partitions.
- **A session timeout shorter than a restart defeats static membership.** With `group.instance.id` set but the timeout exceeded during the restart, the coordinator expires the member and rebalances anyway, so the configuration appears to have no effect.
- **Setting `group.protocol=consumer` does not remove client timing knobs that were already set.** `session.timeout.ms` and `heartbeat.interval.ms` are governed by broker-side group configuration under KIP-848, so tuning the client values has no effect on a group running the new protocol.
- **Increasing partition count changes key placement.** Records for a key land on a different partition after the change, so ordering guarantees per key do not hold across the boundary and stateful consumers may see a key's history split between two partitions.
- **More partitions than consumers is not free, and fewer is a hard cap.** Members beyond the partition count idle indefinitely; adding consumers to a saturated group increases neither throughput nor parallelism.
