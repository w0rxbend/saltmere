---
title: "Kafka Streams state stores: RocksDB locally, changelog topics for truth"
date: 2026-08-13
track: sys-patterns
summary: "Kafka Streams keeps aggregation state in embedded RocksDB stores and makes them fault-tolerant by mirroring every update to a compacted changelog topic. Why groupBy forces a repartition, how standby replicas turn minutes of changelog replay into instant failover, and the three configs (num.standby.replicas, commit.interval.ms, cache size) that decide your recovery and latency behavior. Current as of Kafka 4.3."
reading_time: 5
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

A `count()` over a stream needs somewhere to keep the counts. Kafka Streams' answer is radical locality: state lives in an embedded [RocksDB](/articles/distributed-systems/2026-08-10-lsm-trees-vs-b-trees) instance *inside your application process*, one store per task, no database to call. The interview question is always the same: your process dies and takes its local disk with it — where did the counts go? The answer is the changelog topic, and everything interesting about Kafka Streams operations follows from it.

## Streams, tables, and why an aggregation is a table

A **KStream** is a stream of facts — each record is an independent event ("user X clicked"). A **KTable** is a stream of *updates* — each record overwrites the previous value for its key, like a row UPSERT. They are two views of the same data (Michael Noll's [stream–table duality series](https://www.michael-noll.com/blog/2018/04/05/of-stream-and-tables-in-kafka-and-stream-processing-part1/) is the canonical walkthrough): replaying a changelog stream materializes a table; capturing a table's changes yields a stream. Every stateful DSL operation rides this duality — `aggregate`/`count`/`reduce` turn a KStream into a KTable, and the KTable's backing storage *is* the state store.

## The topology, concretely

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

Two internal topics appear, both named from your `application.id`: `click-counter-clicks-per-url-repartition` and `click-counter-clicks-per-url-changelog`.

## Repartition before you aggregate

Aggregation is per-key and each task owns specific partitions, so all records for a key must reach the same task. `clicks` is keyed by user, but we count by URL — after `groupBy` changes the key, co-partitioning no longer holds. Streams handles this by writing through a **repartition topic**, re-shuffling records so equal URLs land in equal partitions (the same shuffle a [scatter-gather](/articles/sys-patterns/2026-07-26-scatter-gather-pattern) or map-reduce does, but materialized as a Kafka topic). Cost: one extra produce/consume round-trip per record. If your data is *already* keyed correctly, use `groupByKey()` — no repartition. Spotting a needless `groupBy` (or a missed one, which silently gives per-partition partial counts) is a classic code-review interview beat.

## Changelog topics: the store is a cache, the topic is the truth

Every write to `clicks-per-url` is also produced to its **changelog topic** — log-compacted, so it converges to one record per key and stays proportional to state size, not to event history. RocksDB is thus just a local materialization; lose the disk and the task rebuilds by replaying the changelog from scratch. That replay is the operational pain point: restoring a 50 GB store at broker throughput takes tens of minutes, during which the task processes nothing.

Three mitigations, in the order you should reach for them:

1. **`num.standby.replicas: 1`** — another instance consumes the changelog continuously into a warm copy. On failure or rebalance, the standby is promoted with near-zero downtime instead of cold-replaying.
2. **High-availability assignment (KIP-441)** — since Kafka 2.6, a rebalance never hands a stateful task to an instance whose state lags more than `acceptable.recovery.lag` (default 10 000 records); it keeps the task where the state is, spins up *warm-up replicas* (`max.warmup.replicas`) in the background, and moves the task only when they catch up.
3. **Static membership + persistent volumes** — `group.instance.id` plus a reattachable disk means a restarted pod finds its RocksDB files and just resumes. Kafka 4.2 added a Streams-specific broker-side rebalance protocol (KIP-1071) that removes the classic stop-the-world sync barrier; custom assignors for it arrived in 4.3.

## Interactive queries: the store is also your serving layer

Because state is local, you can expose it directly — no read-path database. `streams.store(fromNameAndType("clicks-per-url", keyValueStore()))` answers key lookups from RocksDB; `queryMetadataForKey` tells you *which* instance owns a key so you can add a thin RPC layer for cross-instance routing. For dashboards and "top URLs right now" endpoints this deletes an entire cache tier — the pattern shades into event-sourced read models, but the store here is a byproduct of the aggregation, not the system of record.

## The configs that matter

- **`commit.interval.ms`** (default 30 000 ms) — how often offsets are committed and caches flushed downstream; it bounds both your end-to-end latency and your reprocessing window after a crash. With `processing.guarantee=exactly_once_v2` the default drops to 100 ms, because commit = transaction boundary — Streams EOS is the transactions machinery wired through the topology, covered in [exactly-once in Kafka](/articles/distributed-systems/2026-08-10-delivery-semantics-exactly-once), so state stores, changelogs, and output topics commit atomically.
- **`statestore.cache.max.bytes`** (default 10 MiB per instance) — the write cache that dedups repeated updates per key before they hit RocksDB, the changelog, and downstream. Bigger cache = fewer, larger updates; zero = every single update emitted (useful in tests).
- **`num.standby.replicas`** — see above; the single cheapest availability win for stateful apps.

RocksDB itself is tunable via `rocksdb.config.setter` (memtable and block-cache sizes are the usual suspects — Confluent's [tuning guide](https://www.confluent.io/blog/how-to-tune-rocksdb-kafka-streams-state-stores-performance/) has the details), but reach for it only after the three configs above.

**Try next:** Run two instances of the topology above with `num.standby.replicas=1`, kill -9 one mid-load, and time how long the survivor takes to serve `clicks-per-url` queries — then repeat with standbys disabled and watch the changelog replay.
