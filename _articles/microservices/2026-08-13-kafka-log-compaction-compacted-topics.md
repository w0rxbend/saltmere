---
title: "Kafka Log Compaction: Compacted Topics as Tables, Not Histories"
date: 2026-08-13
track: microservices
summary: "How the log cleaner turns a keyed topic into a latest-value-per-key table: dirty ratio, segment rewriting, tombstones and delete.retention.ms. What compacted topics are for (changelogs, KTables, __consumer_offsets), what they guarantee, and the pitfalls — null keys are rejected and tombstones linger."
reading_time: 6
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

**Gist.** A keyed stream that records the current state of an entity grows without bound, yet time-based retention deletes the current value along with the obsolete ones. Kafka's log compaction removes only records shadowed by a later record with the same key in the same partition, so a full replay reconstructs a **latest-value-per-key table** whose size is proportional to the key space rather than to the number of updates. The cost is that the log stops being an event history: compaction is asynchronous and best-effort, so intermediate values may or may not survive, and deletions must be expressed as **tombstones** whose own removal is governed by a separate grace period.

## Two meanings of a topic

The per-topic configuration `cleanup.policy` decides what a Kafka topic *is*. The default, `delete`, makes it an event stream with a time or size window: whole segments older than `retention.ms` are dropped, regardless of what keys they contained. `compact` makes it a **table in log form** — at least the latest record per key is retained indefinitely, and older values for the same key become eligible for removal. The combination `compact,delete` applies both: compaction removes shadowed records, and the retention cutoff still discards old segments.

Kafka uses this internally. The `__consumer_offsets` topic is compacted, mapping the triple (group, topic, partition) to the latest committed offset; replaying it from the beginning yields the current commit position of every group without replaying every commit ever made.

## How the cleaner works

Each partition log is a sequence of segment files. The **active segment — the one currently being appended to — is never cleaned.** Conceptually the remainder splits into a **cleaned tail**, already compacted and holding at most one live value per key, and a **dirty head**, everything appended since the last cleaning pass.

The log cleaner is a broker-side thread pool, enabled by `log.cleaner.enable=true` by default. Its cycle is:

1. Select the log with the highest **dirty ratio** — dirty bytes divided by total bytes — among those whose ratio exceeds `min.cleanable.dirty.ratio` (default `0.5`, so cleaning starts when half the log is uncompacted).
2. Scan the dirty head to build an in-memory **offset map** holding, for each key, the highest offset at which it appears. The map is allocated from `log.cleaner.dedupe.buffer.size`; if the head contains more distinct keys than the buffer can hold, only a prefix of the head is cleaned in this pass.
3. Recopy the tail and head segments into new segments, dropping every record whose key maps to a strictly later offset, and atomically swap the results in.

Two invariants make the result safe to consume. **Offsets never change.** Compaction leaves gaps in the offset sequence rather than renumbering; a consumer that seeks to offset 5 after offset 5 has been removed is positioned on the next surviving record. And **per-partition ordering is preserved** — surviving records appear in their original relative order, because the rewrite copies rather than reorders.

Two configuration knobs bound the timing. `min.compaction.lag.ms` guarantees a record remains uncompacted for at least that interval, so a consumer reading within the window observes every intermediate update. `max.compaction.lag.ms`, added by **KIP-354**, makes a log eligible for cleaning after that interval even when its dirty ratio is below the threshold; it is the mechanism by which a deletion can be given an upper bound in wall-clock time rather than in write volume.

## Tombstones and delete.retention.ms

A record with a **null value** is a tombstone, meaning "this key is deleted". A cleaning pass removes the key's earlier values, but retains the tombstone itself until it has spent `delete.retention.ms` (default 24 hours) in the *cleaned* portion of the log. During that period a consumer bootstrapping from offset 0 still encounters the deletion marker instead of finding the key silently absent.

The quirk that surprises operators is that **the grace period is measured from when the tombstone's segment is cleaned, not from when the record was produced.** On a low-traffic topic two delays compose: the active segment holds the tombstone until it rolls, which takes up to `segment.ms` (default 7 days), and the log then waits until its dirty ratio crosses `min.cleanable.dirty.ratio`. Tombstones therefore routinely survive far longer than `delete.retention.ms` suggests.

The symmetric hazard runs the other way. **A consumer whose lag exceeds the retention window can miss a tombstone entirely**, observing neither the key nor its deletion. Any consumer that materializes a table must therefore treat two situations as equivalent: a tombstone observed during the stream, and a key absent at the end of a full replay.

## What compacted topics are for

The fitting question is "what is the current value of key K?", never "what happened to K?".

- **Kafka Streams state.** A state store is backed by a changelog topic, compacted for key-value stores and `compact,delete` for windowed ones, and a `GlobalKTable` replays its topic from the beginning on each instance. Restore time scales with the compacted size — the number of live keys — rather than with the number of updates ever written.
- **Kafka's own metadata.** `__consumer_offsets`, along with Kafka Connect's configuration, offset and status topics.
- **Materialized caches and change data capture (CDC).** Replaying a keyed changelog, such as row-level events emitted by Debezium, rebuilds a lookup table or warms a cache without retaining an unbounded log.

What compacted topics are **not** is an event history. The guarantee is *at least* the latest value per key: the dirty head may still hold arbitrarily many stale intermediates, and no bound is offered on which of them survive. Event sourcing on a compacted topic is a category error, because the audit trail is exactly the part compaction is licensed to discard. History requires `cleanup.policy=delete` with sufficient retention, or tiered storage.

### Implementation sketch (Scala)

The cleaner's essential step is the offset map: one pass over the dirty head recording the highest offset per key, then a second pass that keeps a record only if it is the record that pass recorded.

```scala
final case class Record(offset: Long, key: Array[Byte], value: Option[Array[Byte]])

/** Highest offset per key in the dirty head. Keys are wrapped so that
  * array identity does not defeat hashing. */
def offsetMap(head: Iterable[Record]): Map[Seq[Byte], Long] =
  head.foldLeft(Map.empty[Seq[Byte], Long]) { (m, r) =>
    val k = r.key.toSeq
    m.updated(k, math.max(m.getOrElse(k, Long.MinValue), r.offset))
  }

/** Second pass: copy tail ++ head, dropping records shadowed by a later
  * offset for the same key. Offsets of survivors are unchanged, so the
  * output sequence has gaps. */
def compact(segments: Iterable[Record], latest: Map[Seq[Byte], Long]): Vector[Record] =
  segments.iterator
    .filter(r => latest.get(r.key.toSeq).forall(_ == r.offset))
    .toVector

/** Consumer side: materialising the table. A null value retracts the key,
  * and a key never seen is indistinguishable from one whose tombstone
  * was already removed. */
def materialise(replay: Iterable[Record]): Map[Seq[Byte], Array[Byte]] =
  replay.foldLeft(Map.empty[Seq[Byte], Array[Byte]]) { (table, r) =>
    r.value match
      case Some(v) => table.updated(r.key.toSeq, v)
      case None    => table.removed(r.key.toSeq)
  }
```

The `foldLeft` in `materialise` is last-write-wins by construction, which is the property a consumer of a compacted topic must have: duplicates per key are always admissible, so the fold must be idempotent under replay.

## Worked example: a keyed email changelog

```bash
kafka-topics.sh --bootstrap-server localhost:9092 --create --topic user-emails \
  --partitions 6 --replication-factor 3 \
  --config cleanup.policy=compact \
  --config min.cleanable.dirty.ratio=0.1 \
  --config segment.ms=600000 \
  --config delete.retention.ms=3600000

# upper bound on compaction delay (KIP-354)
kafka-configs.sh --bootstrap-server localhost:9092 --alter \
  --entity-type topics --entity-name user-emails \
  --add-config max.compaction.lag.ms=86400000
```

Five keyed records, followed by a cleaning pass:

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

A fresh consumer replaying the topic materialises the current table: u1 maps to alice@new.example and u3 to carol@example. Offsets 2 through 4 keep their original numbers; 0, 1 and eventually 4 are absent, and nothing in the log records that they ever existed.

## Pitfalls

- **Null-key records are rejected on a compact-only topic.** A produce request without a key fails with `InvalidRecordException`, because compaction has no key on which to define shadowing.
- **Compaction lags writes, so duplicates per key are always possible.** The cleaner is asynchronous and never touches the active segment; a consumer that assumes one record per key reads stale values from the dirty head.
- **Changing the partition count or the partitioner splits a key's history.** Latest-per-key holds only within a partition, so records for one key landing in two partitions leave two independently compacted lineages, and a replay can materialise the older one last.
- **A tombstone is not removed on the schedule its name suggests.** The `delete.retention.ms` clock starts at cleaning, so on a low-traffic topic a deleted key remains visible for `segment.ms` plus the time to cross the dirty ratio, plus the retention period.
- **A lagging consumer can skip the deletion.** If the tombstone is removed before the consumer reaches it, the key never appears at all, which is why absence after a full replay must be handled identically to an observed tombstone.
- **Cleaner failure is silent until the disk fills.** A crashed cleaner thread, or an offset map too small for the number of distinct keys in the head (`log.cleaner.dedupe.buffer.size`), leaves the log growing without bound; the `max-dirty-percent` and `uncleanable-partitions-count` metrics are the signals that expose it.
