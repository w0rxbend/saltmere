---
title: "Kafka Streams state stores: RocksDB locally, changelog topics for truth"
date: 2026-08-13
track: sys-patterns
summary: "Kafka Streams keeps aggregation state in embedded RocksDB stores and makes it fault-tolerant by mirroring every update to a compacted changelog topic. Why groupBy forces a repartition, how standby replicas replace cold changelog replay at failover, and the three configurations — num.standby.replicas, commit.interval.ms, state-store cache size — that determine recovery and latency behaviour. Current as of Kafka 4.3."
reading_time: 6
tags: [kafka-streams, state-stores, rocksdb, changelog-topics, standby-replicas]
sources:
  - title: "Apache Kafka docs — Kafka Streams Architecture"
    url: "https://kafka.apache.org/documentation/streams/architecture"
  - title: "Apache Kafka docs — Interactive Queries"
    url: "https://kafka.apache.org/documentation/streams/developer-guide/interactive-queries.html"
  - title: "Confluent Developer — Changelogs and Standbys with Kafka Streams"
    url: "https://developer.confluent.io/courses/kafka-streams/stateful-fault-tolerance/"
  - title: "Confluent blog — Performance Tuning RocksDB for Kafka Streams' State Stores"
    url: "https://www.confluent.io/blog/how-to-tune-rocksdb-kafka-streams-state-stores-performance/"
  - title: "Vasil Kosturski — Exploring Kafka Streams: Partitioning, Scaling, and Fault Tolerance"
    url: "https://vkontech.com/exploring-kafka-steams-partitioning-scaling-and-fault-tolerance/"
---

**Gist.** A stateful stream operation such as `count()` must keep per-key state somewhere, and a stream processor that calls out to a remote database pays a network round-trip per record. Kafka Streams instead holds state in an embedded [RocksDB](/articles/distributed-systems/2026-08-10-lsm-trees-vs-b-trees) instance inside the application process — one store per task — and makes that local state recoverable by mirroring every update to a log-compacted **changelog topic**. The cost is that local disk is now a cache rather than the system of record: when an instance dies, the task cannot resume until its state has been rebuilt from the changelog, and that rebuild is proportional to state size.

## Streams, tables, and why an aggregation is a table

A **KStream** is a stream of facts: each record is an independent event ("user X clicked"). A **KTable** is a stream of *updates*: each record overwrites the previous value for its key, in the manner of a row UPSERT. The two are views of the same data — replaying a changelog stream materializes a table, and capturing a table's changes yields a stream. Michael Noll's [stream–table duality series](https://www.michael-noll.com/blog/2018/04/05/of-stream-and-tables-in-kafka-and-stream-processing-part1/) is the canonical walkthrough. Every stateful operation in the domain-specific language (DSL) rides this duality: `aggregate`, `count` and `reduce` turn a KStream into a KTable, and **the KTable's backing storage is the state store**.

## The topology, concretely

The Kafka Streams DSL is a Java library, so the topology is stated in Java.

```java
StreamsBuilder builder = new StreamsBuilder();

builder.stream("clicks", Consumed.with(Serdes.String(), Serdes.String()))
    // new key != source partition key => Streams inserts a repartition topic
    .groupBy((userId, url) -> url, Grouped.with(Serdes.String(), Serdes.String()))
    .count(Materialized.<String, Long, KeyValueStore<Bytes, byte[]>>as("clicks-per-url")
        .withValueSerde(Serdes.Long()))
    .toStream()
    .to("clicks-per-url-output", Produced.with(Serdes.String(), Serdes.Long()));

Properties props = new Properties();
props.put(StreamsConfig.APPLICATION_ID_CONFIG, "click-counter");   // prefixes internal topics
props.put(StreamsConfig.NUM_STANDBY_REPLICAS_CONFIG, 1);           // default 0
props.put(StreamsConfig.COMMIT_INTERVAL_MS_CONFIG, 1000);          // default 30000
props.put(StreamsConfig.STATESTORE_CACHE_MAX_BYTES_CONFIG, 10 * 1024 * 1024L);
new KafkaStreams(builder.build(), props).start();
```

Two internal topics appear, both named from the configured `application.id`: `click-counter-clicks-per-url-repartition` and `click-counter-clicks-per-url-changelog`.

## Repartition before aggregation

The invariant an aggregation depends on is **all records sharing a key are processed by the same task**. Aggregation is per-key and each task owns a fixed set of input partitions, so that invariant holds only while the record key agrees with the partitioning key. The `clicks` topic is keyed by user, while the aggregation counts by URL; once `groupBy` changes the key, co-partitioning no longer holds and each task would see an arbitrary subset of a URL's clicks.

Kafka Streams restores the invariant by writing through a **repartition topic**, re-shuffling records so that equal URLs land in equal partitions — the same shuffle a [scatter-gather](/articles/sys-patterns/2026-07-26-scatter-gather-pattern) or map-reduce performs, materialized as a Kafka topic. The cost is **one extra produce-and-consume round-trip per record**. When the input is already keyed correctly, `groupByKey()` performs the aggregation with no repartition. The failure mode of getting this wrong is quiet: a missing repartition yields per-partition partial counts rather than an error.

## Changelog topics: the store is a cache, the topic is the truth

Every write to `clicks-per-url` is also produced to its **changelog topic**. That topic is log-compacted, so it converges toward one record per key and its size stays proportional to the size of the state rather than to the length of the event history. RocksDB is therefore a local materialization of the changelog. If the disk is lost, the task rebuilds its store by replaying the changelog from the beginning.

Replay is the operational pain point. Restoration proceeds at broker read throughput, and **the task processes no input records while restoring** — a large store therefore converts an instance failure into an extended processing gap for the partitions that instance owned.

Three mitigations, in increasing order of operational commitment:

1. **`num.standby.replicas: 1`** — a second instance consumes the changelog continuously into a warm copy of the store. On failure or rebalance the standby is promoted, replacing cold replay with a takeover of state that is already close to current.
2. **High-availability task assignment (KIP-441)** — since Kafka 2.6, a rebalance does not hand a stateful task to an instance whose state lags by more than `acceptable.recovery.lag` (default 10 000 records). The task stays where the state already is, **warm-up replicas** (bounded by `max.warmup.replicas`) are built in the background, and the task moves only once a warm-up replica has caught up.
3. **Static membership plus persistent volumes** — `group.instance.id` together with a reattachable disk means a restarted instance finds its existing RocksDB files and resumes from them. A Streams-specific broker-coordinated rebalance protocol (KIP-1071) moves task assignment from the leader client to the group coordinator; it is recent enough that the release in which it stops being early access should be checked against the Kafka release notes rather than assumed.

## Interactive queries: the store as a serving layer

Because state is local, it can be read directly rather than through a separate read-path database. `streams.store(fromNameAndType("clicks-per-url", keyValueStore()))` answers key lookups from RocksDB, and `queryMetadataForKey` reports **which instance owns a given key**, which is what a thin remote-procedure-call layer needs in order to route a cross-instance query. For dashboards and "top URLs now" endpoints this removes a cache tier. The pattern resembles an event-sourced read model, with the difference that the store here is a byproduct of the aggregation rather than the system of record.

## The configurations that matter

- **`commit.interval.ms`** (default 30 000 ms) — the period at which offsets are committed and caches are flushed downstream. It bounds both end-to-end latency and the amount of work reprocessed after a crash. Under `processing.guarantee=exactly_once_v2` the default drops to 100 ms, since a commit is a transaction boundary; exactly-once semantics in Streams are the Kafka transaction machinery wired through the topology, covered in [exactly-once in Kafka](/articles/distributed-systems/2026-08-10-delivery-semantics-exactly-once), so that state stores, changelogs and output topics commit atomically.
- **`statestore.cache.max.bytes`** (default 10 MiB per instance) — the write cache that collapses repeated updates to the same key before they reach RocksDB, the changelog and downstream operators. A larger cache produces fewer and larger updates; setting it to zero emits every intermediate update, which makes output deterministic for tests.
- **`num.standby.replicas`** (default 0) — as above: the change with the largest effect on failover time for a stateful application.

RocksDB itself is tunable through `rocksdb.config.setter`, with memtable and block-cache sizes as the usual subjects; Confluent's [tuning guide](https://www.confluent.io/blog/how-to-tune-rocksdb-kafka-streams-state-stores-performance/) documents the parameters. That tuning is worth reaching for after the three configurations above, not before.

## Pitfalls

- **`groupBy` on an already-correct key doubles the write path.** Symptom: an unexplained repartition topic and roughly twice the broker traffic. Cause: `groupBy` always marks the key as changed, so Streams inserts a repartition topic even when the new key equals the old one; `groupByKey()` does not.
- **A missing repartition produces plausible but wrong aggregates.** Symptom: counts that are lower than expected and vary with partition count, with no error logged. Cause: records for one key are spread across tasks, so each task aggregates only the subset it owns.
- **Deleting local state to "fix" a stuck instance triggers a full replay.** Symptom: the instance starts but processes nothing for a long interval after the state directory is cleared. Cause: the store is rebuilt from the changelog from the beginning, at broker read throughput, before processing resumes.
- **Standby replicas require a second instance to exist.** Symptom: `num.standby.replicas=1` is configured yet failover still replays the changelog. Cause: with a single instance in the group there is nowhere to place a standby task.
- **Enlarging `statestore.cache.max.bytes` changes visible output as well as throughput.** Symptom: intermediate update records disappear from downstream topics and tests that assert on them fail. Cause: the cache deduplicates updates per key between flushes, so only the last value in each interval is emitted.
- **Non-compacted changelog topics break the size guarantee.** Symptom: changelog retention grows with event volume rather than with key cardinality, and restore time grows with it. Cause: compaction is a topic-level policy; a manually pre-created changelog topic without `cleanup.policy=compact` retains the full update history.
