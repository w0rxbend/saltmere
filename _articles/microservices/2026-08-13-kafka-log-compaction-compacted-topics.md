---
title: "Kafka Log Compaction: Compacted Topics as Tables, Not Histories"
date: 2026-08-13
track: microservices
summary: "How the log cleaner turns a keyed topic into a latest-value-per-key table: dirty ratio, segment rewriting, tombstones and delete.retention.ms. What compacted topics are for (changelogs, KTables, __consumer_offsets), what they guarantee, and the pitfalls — null keys are rejected and tombstones linger."
reading_time: 5
tags: [kafka, log-compaction, compacted-topics, tombstones, changelog]
sources:
  - title: "Log Compaction — Apache Kafka design documentation"
    url: "https://kafka.apache.org/documentation/#compaction"
  - title: "KIP-354: Add a Maximum Log Compaction Lag"
    url: "https://cwiki.apache.org/confluence/display/KAFKA/KIP-354%3A+Add+a+Maximum+Log+Compaction+Lag"
  - title: "Understanding Kafka Compaction (Ted Naleid)"
    url: "https://www.naleid.com/2023/07/30/understanding-kafka-compaction.html"
  - title: "Kafka quirks: tombstones that refuse to disappear (Javier Holguera)"
    url: "https://javierholguera.com/2020/02/17/kafka-quirks-tombstones-that-refuse-to-disappear/"
  - title: "Kafka log compaction: configuration and troubleshooting (Redpanda)"
    url: "https://www.redpanda.com/guides/kafka-performance-kafka-log-compaction"
---

`cleanup.policy` decides what a Kafka topic *is*. `delete` (the default) makes it an event stream with a time/size window: segments older than `retention.ms` are dropped wholesale. `compact` makes it a **table in log form**: Kafka keeps at least the latest record per key forever, discarding older values for the same key. `compact,delete` does both — compaction plus a retention cutoff. Kafka itself runs on this: `__consumer_offsets` is a compacted topic mapping (group, topic, partition) to the latest committed offset.

## How the cleaner actually works

Each partition log is a sequence of segments; the **active segment is never cleaned**. Conceptually the log splits into a **cleaned tail** (already compacted, at most one live value per key) and a **dirty head** (everything appended since the last clean).

The log cleaner (a broker-side thread pool, `log.cleaner.enable=true` by default) repeatedly:

1. Picks the log with the highest **dirty ratio** — dirty bytes / total bytes — among logs whose ratio exceeds `min.cleanable.dirty.ratio` (default 0.5, i.e. clean when half the log is uncompacted).
2. Scans the dirty head to build an in-memory **offset map**: latest offset per key.
3. Recopies tail + head segments, dropping any record shadowed by a later offset for the same key, and swaps in the compacted segments.

Two crucial invariants: **offsets never change** (compaction leaves gaps, it never renumbers — a consumer seeking offset 5 lands on the next surviving record), and **per-partition ordering is preserved**. Timing knobs: `min.compaction.lag.ms` guarantees a record stays uncompacted for at least that long (readers of the head see every intermediate update within the window); `max.compaction.lag.ms` (KIP-354, default effectively infinite) forces a log to become eligible even below the dirty ratio — the tool for GDPR-style "deletes must actually happen within N days".

## Tombstones and delete.retention.ms

A record with a **null value** is a tombstone: "delete this key". Compaction drops the key's older values, and eventually the tombstone itself — but only after it has been in the *cleaned* portion for `delete.retention.ms` (default 24 h). That grace period exists so a consumer bootstrapping from offset 0 can still observe the deletion rather than simply never seeing the key.

The classic quirk: that clock starts when the tombstone's segment gets *cleaned*, not when it was produced. On a low-traffic topic the dirty ratio may not be crossed for days and the active segment may take `segment.ms` (default 7 days) to roll, so tombstones routinely outlive `delete.retention.ms` by a wide margin. Conversely, consumers that lag more than the retention window can miss tombstones entirely — table-building consumers must treat both "tombstone seen" and "key absent after full replay" as deletes.

## What compacted topics are for

Changelog/table semantics — anywhere the question is "what is the current value of key K?", not "what happened?":

- **Kafka Streams state**: every KTable and state store is backed by a compacted changelog topic; a `GlobalKTable` replays one from the beginning on every instance. Restore time depends on compacted size (keys), not history length.
- **Kafka's own metadata**: `__consumer_offsets`, Connect's config/offset/status topics.
- **Materialized caches / CDC**: replay a compacted keyed changelog (e.g. row-level events from Debezium, covered separately) to rebuild a lookup table or warm a cache without an unbounded log.

What they are **not**: an event history. The guarantee is *at least the latest value per key* (the head may still hold stale intermediates) — never a full audit trail. Event sourcing on a compacted topic is a category error; use normal retention or tiered storage for history.

## Pitfalls worth naming in an interview

- **Null-key records are rejected.** On a `compact`-only topic the broker fails produces without a key (`InvalidRecordException`) — compaction is meaningless without one.
- **Compaction lags writes.** The cleaner is asynchronous; duplicates per key are always possible. Consumers must be last-write-wins ("table semantics"), never assume one record per key.
- **Keys must keep partitioning consistently.** Latest-per-key only holds within a partition; changing partition count or partitioner splits a key's history across partitions.
- **Cleaner health is invisible until it isn't.** A crashed cleaner thread or an offset map too small (`log.cleaner.dedupe.buffer.size`) lets logs grow unbounded — watch `max-dirty-percent` and `uncleanable-partitions-count` metrics.

## Worked example: a keyed email changelog

```bash
kafka-topics.sh --bootstrap-server localhost:9092 --create --topic user-emails \
  --partitions 6 --replication-factor 3 \
  --config cleanup.policy=compact \
  --config min.cleanable.dirty.ratio=0.1 \
  --config segment.ms=600000 \
  --config delete.retention.ms=3600000

# force an upper bound on compaction delay (KIP-354)
kafka-configs.sh --bootstrap-server localhost:9092 --alter \
  --topic user-emails --add-config max.compaction.lag.ms=86400000
```

Produce five keyed records, then let the cleaner run:

```text
offset  key   value
0       u1    alice@old.example
1       u2    bob@example
2       u1    alice@new.example      # shadows offset 0
3       u3    carol@example
4       u2    <null>                 # tombstone: delete u2

after compaction:        2:u1  3:u3  4:u2=<null>
after delete.retention:  2:u1  3:u3
```

A fresh consumer replaying the topic materializes exactly the current table: u1 → alice@new.example, u3 → carol@example. Offsets 2–4 kept their original numbers; 0, 1, and eventually 4 are simply gone.

**Try next:** create the topic above with those aggressive settings on a local broker, produce updates and a tombstone, and use `kafka-dump-log.sh` on the segment files to watch the cleaned tail and dirty head evolve.
