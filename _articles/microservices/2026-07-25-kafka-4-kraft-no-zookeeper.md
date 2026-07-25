---
title: "Kafka 4.0: ZooKeeper is gone, KRaft is the only mode"
date: 2026-07-25
track: microservices
summary: "Apache Kafka 4.0 (18 March 2025) is the first major release to run entirely without ZooKeeper. KRaft — a Raft-based controller quorum that stores metadata in Kafka itself — is now the only mode. Here's what changes operationally: no separate ensemble, a formatted cluster UUID, and process.roles on every node."
reading_time: 5
tags: [kafka, kraft, zookeeper, kip-500, kip-848, operations]
sources:
  - title: "Apache Kafka 4.0.0 Release Announcement (18 Mar 2025)"
    url: "https://kafka.apache.org/blog/2025/03/18/apache-kafka-4.0.0-release-announcement/"
  - title: "KIP-500: Replace ZooKeeper with a Self-Managed Metadata Quorum"
    url: "https://cwiki.apache.org/confluence/display/KAFKA/KIP-500%3A+Replace+ZooKeeper+with+a+Self-Managed+Metadata+Quorum"
  - title: "KIP-848: The Next Generation of the Consumer Rebalance Protocol"
    url: "https://cwiki.apache.org/confluence/display/KAFKA/KIP-848%3A+The+Next+Generation+of+the+Consumer+Rebalance+Protocol"
  - title: "Confluent — Apache Kafka 4.0 release"
    url: "https://www.confluent.io/blog/latest-apache-kafka-release/"
---

For a decade, running Kafka meant running *two* distributed systems: the brokers, and a ZooKeeper ensemble holding cluster metadata — topics, partitions, ACLs, controller elections. Two failure domains, two upgrade cadences, two things to page you at 3am. **Apache Kafka 4.0**, released **18 March 2025**, ends that. It's the first major release to operate entirely without ZooKeeper. KRaft (KIP-500) isn't just the default now — it's the *only* mode. There is no ZooKeeper code to fall back to.

## What KRaft actually replaces

KRaft moves metadata *into Kafka*. A subset of nodes form a **controller quorum** that runs a Raft consensus protocol over an internal `__cluster_metadata` topic. The active controller is the quorum leader; brokers replicate the metadata log like any other topic and cache it locally. The upshot: leader elections and metadata propagation stop being a ZooKeeper round-trip and become a log fetch, so a cluster with millions of partitions recovers in seconds instead of minutes. Every node now declares what it is via `process.roles`.

Because there's no ensemble to point at, a cluster is bootstrapped by *formatting storage* with a shared cluster UUID before first start — a step ZooKeeper used to do implicitly.

```bash
# 1. Mint one cluster ID (do this once, reuse it on every node)
KAFKA_CLUSTER_ID="$(bin/kafka-storage.sh random-uuid)"

# 2. Format each node's log dirs against that ID + its config
bin/kafka-storage.sh format \
    --cluster-id "$KAFKA_CLUSTER_ID" \
    --config config/server.properties
```

A minimal single-node `server.properties` for a combined broker+controller looks like this. In production you separate the roles, but combined mode makes the moving parts obvious:

```properties
process.roles=broker,controller
node.id=1
controller.quorum.voters=1@localhost:9093
listeners=PLAINTEXT://:9092,CONTROLLER://:9093
controller.listener.names=CONTROLLER
inter.broker.listener.name=PLAINTEXT
log.dirs=/var/lib/kafka/data
```

`controller.quorum.voters` lists the quorum as `nodeId@host:port` entries — three or five voters in production for fault tolerance (a 3-node quorum tolerates one loss, 5 tolerates two). Kafka 4.1 later added *dynamic* quorums via `controller.quorum.bootstrap.servers`, letting you add and remove controllers with `kafka-metadata-quorum.sh` without editing every config — but the static form above is the 4.0 baseline and still valid.

## The other operational changes

Two more things will bite you on upgrade:

- **Java 17 is required for brokers, Connect, and tools** (clients and Streams still run on Java 11). If your broker hosts are on Java 11, fix that before touching Kafka 4.0.
- **The next-gen consumer rebalance protocol (KIP-848) is now GA** and enabled server-side by default. It moves rebalancing off the "stop-the-world" model — the coordinator drives partition moves incrementally, so a scaling event no longer freezes the whole group. Consumers still opt in per-app with `group.protocol=consumer`; leave it unset and you get the classic protocol.

There is no in-place "just restart into KRaft" from a ZooKeeper cluster on 4.0 — you migrate on a 3.x bridge release *first*, then upgrade. Jumping a ZooKeeper-based cluster straight to 4.0 is not supported. (Also new in 4.0: **Queues for Kafka**, KIP-932, shipped as *early access* — share groups for queue-style consumption, not yet production-GA.)

**Try next:** run the two commands above against a fresh `config/server.properties` from the 4.0 tarball, start the single node, then `bin/kafka-metadata-quorum.sh --bootstrap-server localhost:9092 describe --status` to watch the Raft quorum — leader, epoch, and high-water mark — with not a single ZooKeeper process in sight.
