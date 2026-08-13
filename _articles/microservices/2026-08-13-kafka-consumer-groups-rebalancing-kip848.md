---
title: "Kafka Consumer Groups and Rebalancing: From Stop-the-World to KIP-848"
date: 2026-08-13
track: microservices
summary: "Why classic Kafka rebalances freeze the whole group, how cooperative sticky assignment (KIP-429) and static membership (KIP-345) patched the pain, and how KIP-848's broker-driven incremental protocol — GA since Kafka 4.0, opt-in via group.protocol=consumer — replaces the dance entirely."
reading_time: 5
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

A consumer group divides a topic's partitions among its members: each partition is owned by exactly one consumer in the group at a time. Any change to that mapping is a **rebalance**, and rebalances are where most "Kafka is slow/duplicating" incidents in interviews (and production) come from.

## How assignment works — the classic protocol

Each group has a **group coordinator**: the broker hosting the group's partition of `__consumer_offsets`. In the classic, client-driven protocol, a rebalance is a two-phase dance: every member sends `JoinGroup`, the coordinator picks one consumer as *group leader*, that consumer runs the assignor (`partition.assignment.strategy`: range by default, plus round-robin, sticky, cooperative-sticky) and distributes the plan via `SyncGroup`. Rebalances trigger on member join/leave, session timeout (`session.timeout.ms`), a poll loop exceeding `max.poll.interval.ms`, or subscription/partition-count changes.

## Why rebalances hurt

The original **eager** protocol is stop-the-world: on every rebalance, *every* member revokes *all* its partitions before the new assignment is computed, even if 90% of partitions end up back where they were. During that window nothing in the group consumes — end-to-end lag spikes across every partition. Members that can't commit before revocation reprocess records afterward, so each rebalance is also a duplicate-delivery event (your handlers had better be idempotent). Stateful consumers (Kafka Streams) additionally flush and possibly restore state. Worse, one slow member stalls everyone, and a flapping consumer (GC pause past the session timeout, crash loop) causes cascading rebalances. Interview soundbite: eager rebalancing turns one member's failure into the whole group's outage.

## Patch #1: cooperative sticky rebalancing (KIP-429)

Since Kafka 2.4, `CooperativeStickyAssignor` implements **incremental cooperative rebalancing**: members keep their current partitions through the rebalance and revoke only the ones actually moving, at the cost of a second, quick rebalance round to hand the revoked partitions to their new owners. Consumption on unaffected partitions never stops. This became the recommended assignor for the classic protocol — but it's still client-driven, still funnels assignment through one group-leader consumer, and still synchronizes the whole group on each round.

## Patch #2: static membership (KIP-345)

Since 2.3, setting a stable `group.instance.id` per consumer instance makes membership **static**: a restart that completes within `session.timeout.ms` reclaims the same member identity and assignment with no rebalance at all. Pair it with a generous session timeout (tens of seconds to minutes). It's the standard fix for Kubernetes rolling restarts of stateful consumers — StatefulSet ordinal in, rebalance storm out.

## The real fix: KIP-848

KIP-848, GA in Kafka 4.0, deletes the client-side dance entirely:

- **Broker-driven**: the group coordinator tracks subscriptions and metadata, computes the target assignment server-side (pluggable server-side assignors; `uniform` and `range` ship by default, selectable via `group.remote.assignor`), and no consumer is "group leader" anymore.
- **Incremental, no global barrier**: reconciliation happens per member through the regular heartbeat. The coordinator tells each consumer individually what to revoke or acquire; unaffected members never pause.
- **Fewer client knobs**: `partition.assignment.strategy`, `session.timeout.ms`, and `heartbeat.interval.ms` move to broker-side group configs (`group.consumer.session.timeout.ms`, etc.).

Status check (verified August 2026, Kafka 4.3.x current): the protocol is production-ready but **opt-in** — clients set `group.protocol=consumer`, while `classic` remains the client default for compatibility. Kafka 4.3 (KIP-1274) now logs a deprecation-style warning when a consumer starts on the classic protocol. Mixed groups work: the coordinator upgrades a classic group when a new-protocol member joins and bridges the old members, so you can migrate a live group with a rolling deploy. Kafka Streams gets its own server-side variant (KIP-1071, GA in 4.2). The measurable win: vendors benchmark large-group rebalances completing an order of magnitude faster, and a single flapping member now disturbs only the partitions it owned.

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

## Choosing partition counts

Partition count is the group's **maximum parallelism**: an N-partition topic feeds at most N active consumers per group; extras sit idle. A working method:

1. Estimate target throughput and measured per-consumer throughput; partitions ≥ target / per-consumer, then add headroom (2–3×) because *keys hash to partitions* — adding partitions later changes key placement and breaks per-key ordering during the transition.
2. Cap the excess: every partition costs open files, replication fetch traffic, more end-to-end latency (leader must replicate more, smaller batches), and — under the classic protocol — bigger, slower rebalances. Dozens per topic is normal; thousands need justification.
3. Prefer numbers with many divisors (12, 24, 30) so consumers divide evenly at several group sizes.

If you're reaching for huge partition counts just to scale slow per-message work, that's queue-shaped — Kafka's share groups (KIP-932, GA in 4.2) decouple parallelism from partitions and are covered in a separate article.

**Try next:** start three consumers on a 12-partition topic, kill one mid-load, and compare rebalance logs and pause time with `group.protocol=classic` (eager assignor) vs `group.protocol=consumer` on a Kafka 4.x cluster.
