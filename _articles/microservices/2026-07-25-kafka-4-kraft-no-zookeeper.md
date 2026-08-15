---
title: "Kafka 4.0: ZooKeeper removed, KRaft the only mode"
date: 2026-07-25
track: microservices
summary: "Apache Kafka 4.0 (18 March 2025) is the first major release to run entirely without ZooKeeper. KRaft — a Raft-based controller quorum that stores metadata in Kafka itself — is the only mode. The operational consequences: no separate ensemble, a formatted cluster UUID, and process.roles on every node."
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

**Gist.** Running Apache Kafka historically meant operating two distributed systems: the brokers, and a ZooKeeper ensemble holding cluster metadata — topics, partitions, access control lists (ACLs), controller elections — with two failure domains and two upgrade cadences. **Kafka 4.0, released 18 March 2025**, removes ZooKeeper entirely: metadata now lives in an internal Kafka log replicated by a Raft-based **controller quorum** (KRaft, specified in KIP-500), which is the only supported mode. The cost is that cluster identity and quorum membership become explicit operator inputs — storage must be formatted with a cluster UUID before first start, every node must declare `process.roles`, and a ZooKeeper-based cluster cannot be upgraded directly to 4.0.

## What KRaft replaces

Under ZooKeeper, metadata was held outside Kafka in a separate replicated store, and a single elected controller broker read that state and pushed changes to the other brokers. Under KRaft the metadata **is a Kafka log**: an internal topic named `__cluster_metadata`, replicated by a designated set of controller nodes running the Raft consensus protocol among themselves.

The structure that follows from this is worth stating precisely:

- **The active controller is the Raft leader of the metadata quorum.** A metadata change — a topic creation, a partition leader change, an ACL update — is an append to the metadata log. It is committed once a majority of voters have replicated it, and only then does it become visible cluster state.
- **Brokers are observers of that log, not participants in the election.** A broker fetches `__cluster_metadata` the way a follower fetches any partition and applies the records to a local in-memory view. **Metadata propagation therefore becomes a log fetch rather than a round trip to an external store**, and a broker's position in the log — its metadata offset — is a measurable indication of how stale its view is.
- **State transfer is incremental.** Because the log is ordered, a rejoining broker resumes from its last applied offset instead of re-reading a full snapshot of cluster state. KIP-500 gives scaling to clusters with a far larger number of partitions than the ZooKeeper arrangement supported as a goal of the design; the release material does not publish a recovery-time measurement to accompany it.

The invariant that makes the arrangement safe is the ordinary Raft one: **a record that has been committed to the metadata log is present on a majority of voters and is never removed**, so a newly elected controller's log already contains every committed metadata change. There is no second source of truth to reconcile against, which is the structural difference from the ZooKeeper arrangement rather than a mere reduction in process count.

## Bootstrapping without an ensemble

With no external ensemble to register against, cluster identity has to be supplied by the operator. A KRaft cluster is bootstrapped by **formatting each node's log directories against a shared cluster UUID** before first start.

```bash
# 1. Mint one cluster ID; it is generated once and reused on every node
KAFKA_CLUSTER_ID="$(bin/kafka-storage.sh random-uuid)"

# 2. Format each node's log dirs against that ID plus its own config
bin/kafka-storage.sh format \
    --cluster-id "$KAFKA_CLUSTER_ID" \
    --config config/server.properties
```

The identifier is written into the log directories. Nodes formatted with different identifiers do not form one cluster; they form two, each convinced the other is a stranger.

A minimal single-node configuration for a combined broker-plus-controller node makes the moving parts visible. Production deployments separate the roles onto distinct nodes.

```properties
process.roles=broker,controller
node.id=1
controller.quorum.voters=1@localhost:9093
listeners=PLAINTEXT://:9092,CONTROLLER://:9093
controller.listener.names=CONTROLLER
inter.broker.listener.name=PLAINTEXT
log.dirs=/var/lib/kafka/data
```

Three properties carry the load. `process.roles` declares whether a node is a broker, a controller, or both. `node.id` is the node's identity within the cluster and appears in the voter list. `controller.quorum.voters` enumerates the quorum as `nodeId@host:port` entries, and because commitment requires a majority, **a quorum of *n* voters tolerates the loss of ⌊(n−1)/2⌋ voters** — one loss at three voters, two at five. A controller listener must exist and be named in `controller.listener.names`; controller traffic does not share the inter-broker listener.

A later alternative exists: a cluster formatted for dynamic quorums discovers controllers through `controller.quorum.bootstrap.servers` and changes membership with `kafka-metadata-quorum.sh` rather than by editing the voter list in every configuration file. The static `controller.quorum.voters` form above remains valid and is the simpler arrangement to reason about.

## Other changes that surface on upgrade

- **Java 17 is required for brokers, Connect and the command-line tools.** Clients and Kafka Streams continue to run on Java 11. Broker hosts still on Java 11 must be moved before the Kafka upgrade, not during it.
- **The next-generation consumer rebalance protocol (KIP-848) is generally available** and enabled on the server side by default. It replaces the stop-the-world rebalance model: the group coordinator drives partition reassignment incrementally, so a scaling event does not suspend consumption for the whole group. **Consumers opt in per application with `group.protocol=consumer`**; when the property is unset, the classic protocol is used.
- **Queues for Kafka (KIP-932) ships as early access**, providing share groups for queue-style consumption. Early access is not a general-availability guarantee.
- **There is no in-place restart from a ZooKeeper-based cluster into 4.0.** Migration is performed on a 3.x bridge release first; the 4.0 upgrade follows. Upgrading a ZooKeeper-based cluster straight to 4.0 is not supported.

The quorum's own state is inspectable at runtime. Against a running node, `bin/kafka-metadata-quorum.sh --bootstrap-server localhost:9092 describe --status` reports the current leader, the leader epoch and the high-water mark of the metadata log — the three values that describe which controller is authoritative and how far the committed metadata extends.

## Pitfalls

- **Formatting nodes with separately generated cluster UUIDs produces two clusters that never converge.** `kafka-storage.sh random-uuid` is run once, and its output is reused for every format; running it per node writes a different identity into each set of log directories.
- **An even number of voters buys no extra fault tolerance.** Four voters still require three for a majority and so tolerate one loss, the same as three voters, while adding a node whose failure must be replicated around.
- **Deleting a controller's log directories deletes metadata replicas, not cached data.** Metadata durability now depends on the surviving majority of controller log directories, so treating them as a rebuildable cache loses committed cluster state once the majority is gone.
- **Setting `group.protocol=consumer` on applications does not by itself obtain the new protocol.** A broker that does not implement or has not enabled it cannot serve the new request types, so the client property is not self-sufficient.
- **Planning a direct ZooKeeper-to-4.0 jump strands the cluster mid-upgrade**, because 4.0 contains no ZooKeeper code path to fall back to; the migration must complete on a 3.x bridge release first.
- **Reusing the inter-broker listener for controller traffic leaves `controller.listener.names` unsatisfied and the configuration fails validation at startup**, since the controller endpoint is a distinct listener in the KRaft configuration model.
