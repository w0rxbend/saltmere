---
title: "Change Data Capture with Debezium: the Transaction Log as an Event Stream"
date: 2026-07-31
track: microservices
summary: "Turning the Postgres WAL or MySQL binlog into an ordered stream of change events on Kafka with Debezium — log-based CDC, the before/after/op/source envelope, snapshot versus streaming, and the relation to the outbox pattern."
reading_time: 6
tags: [debezium, cdc, kafka, postgres, microservices, event-driven]
sources:
  - title: "Debezium 3.6 Release Summary"
    url: "https://debezium.io/blog/2026/07/01/debezium-3-6-final-release/"
  - title: "Debezium connector for PostgreSQL (reference docs)"
    url: "https://debezium.io/documentation/reference/stable/connectors/postgresql.html"
  - title: "Outbox Event Router :: Debezium Documentation"
    url: "https://debezium.io/documentation/reference/stable/transformations/outbox-event-router.html"
  - title: "New Record State Extraction (event flattening) :: Debezium Documentation"
    url: "https://debezium.io/documentation/reference/stable/transformations/event-flattening.html"
---

**Gist.** A service that owns a database and must notify other services of changes typically performs a dual write: commit the row, then publish a message. The two steps are not atomic, so a crash between them either loses a message or emits a phantom event for a transaction that rolled back. Change Data Capture (CDC) removes the second write by reading the database's own write-ahead log and emitting every committed change as an event; the cost is an external replication client that pins log segments the database would otherwise recycle.

## Log-based CDC compared with polling and triggers

Three mechanisms detect row changes, and they differ in what they can observe.

**Polling** issues `SELECT ... WHERE updated_at > ?` on a schedule. It observes no deletes, because a deleted row has no later timestamp to find; it adds query load to the same tables serving traffic; and its detection latency is bounded below by the poll interval.

**Triggers** fire on INSERT, UPDATE and DELETE and append to an audit table. They observe every operation, but they execute **inside the writing transaction**, so their cost is paid on the write path, and every schema change requires a corresponding trigger change.

**Log-based CDC** reads the log the database already maintains for crash recovery and replication — the PostgreSQL write-ahead log (WAL) or the MySQL binary log (binlog). Every committed transaction appears there **in commit order**, deletes included. Debezium attaches as a replication client rather than a query client, so it does not contend for the tables being captured, and a change becomes observable as soon as its commit record is decoded rather than at the next poll.

## Deployment shapes

Debezium runs in one of two shapes. As a **Kafka Connect** plugin it executes inside a Connect cluster and writes to Kafka topics directly; this is the common path where Kafka is already operated. As **Debezium Server** it runs standalone and sinks to other targets — Kinesis, Pulsar, Google Pub/Sub, NATS, Amazon SNS and others — with no Connect cluster. The release documented here is **Debezium 3.6.Final, released 1 July 2026**.

## The PostgreSQL connector

PostgreSQL must run with `wal_level = logical`; without it the WAL records row identifiers but not enough column detail for logical decoding. Debezium uses the built-in `pgoutput` decoding plugin, available without extra libraries since PostgreSQL 10, plus two server-side objects: a **replication slot**, which records the log sequence number (LSN) the client has confirmed, and a **publication**, which enumerates the captured tables.

```json
{
  "name": "inventory-connector",
  "config": {
    "connector.class": "io.debezium.connector.postgresql.PostgresConnector",
    "database.hostname": "postgres",
    "database.port": "5432",
    "database.user": "debezium",
    "database.password": "${file:/secrets/connect.properties:pg_password}",
    "database.dbname": "inventory",
    "topic.prefix": "inventory",
    "plugin.name": "pgoutput",
    "slot.name": "dbz_inventory_slot",
    "publication.name": "dbz_inventory_pub",
    "table.include.list": "public.orders,public.customers",
    "snapshot.mode": "initial"
  }
}
```

Each captured table receives its own topic, here `inventory.public.orders`. The slot is the load-bearing piece of state: **PostgreSQL cannot recycle WAL segments beyond a slot's confirmed position**, so a connector that is stopped, crashed, or merely lagging causes unbounded WAL growth on the primary. The view `pg_replication_slots` exposes the confirmed position and is the quantity to alert on.

## The change-event envelope

Every event carries the same envelope, so a consumer can dispatch on operation rather than on table-specific shape.

```json
{
  "op": "u",
  "ts_ms": 1753900000000,
  "before": { "id": 42, "status": "PENDING",  "total": 90 },
  "after":  { "id": 42, "status": "SHIPPED",  "total": 90 },
  "source": {
    "connector": "postgresql", "db": "inventory", "schema": "public",
    "table": "orders", "lsn": 34567890, "txId": 4211, "snapshot": false
  }
}
```

- **`op`** — the operation: `c` create (INSERT), `u` update, `d` delete, `r` read (a snapshot row), `t` truncate.
- **`before` / `after`** — row state before and after the change. An insert has a null `before`; a delete has a null `after` and is followed by a **tombstone**, a record with the same key and a null value, which lets a log-compacted topic discard the key entirely.
- **`source`** — provenance: connector, table, WAL position (`lsn`), transaction identifier, and a `snapshot` flag identifying which phase produced the event.

Where a consumer needs only the current row state, the **New Record State Extraction** single-message transform (`ExtractNewRecordState`) flattens the envelope to the `after` state.

## Snapshot phase and streaming phase

On first start the connector performs a **snapshot**: a consistent read of every included table, emitting each existing row as an `op: r` event. A consumer therefore receives the full current state rather than only changes occurring after the connector was created. Once the snapshot completes the connector enters the **streaming phase**, reading the WAL from the slot's recorded position and emitting `c`, `u`, `d` and `t` events per committed transaction. `snapshot.mode` selects the behaviour; `initial` snapshots once and then streams, while other modes skip the snapshot or take a schema-only snapshot carrying no rows.

Because both phases publish to the same topic, and because a connector restart replays from the last confirmed LSN, **consumers observe at-least-once delivery** and must tolerate duplicates.

### Implementation sketch (Scala)

The consumer-side invariant is that applying an event twice must equal applying it once. The `source.lsn` is monotonic within a connector, so it doubles as a per-key version that rejects replays.

```scala
enum Op:
  case Create, Update, Delete, Read, Truncate

final case class Envelope(
    key: String,
    op: Op,
    lsn: Long,
    after: Option[Map[String, String]]
)

/** Last LSN applied per key; replays below it are dropped. */
final class Projection:
  private var rows: Map[String, Map[String, String]] = Map.empty
  private var applied: Map[String, Long] = Map.empty

  def apply(e: Envelope): Unit =
    if applied.get(e.key).exists(_ >= e.lsn) then () // already applied
    else
      e.op match
        case Op.Create | Op.Update | Op.Read =>
          e.after.foreach(row => rows = rows.updated(e.key, row))
        case Op.Delete =>
          rows = rows.removed(e.key)
        case Op.Truncate =>
          rows = Map.empty
      applied = applied.updated(e.key, e.lsn)

  def get(key: String): Option[Map[String, String]] = rows.get(key)
```

A tombstone, whose value is null rather than an envelope, carries no LSN and is not fed to `apply`; it exists for the broker's compaction, not for the projection.

## Relation to the transactional outbox

The outbox pattern addresses the same dual-write problem: the business transaction also inserts a row into an `outbox` table, and a relay publishes those rows. CDC and the outbox are complementary, and Debezium supports both.

- **Raw table capture** streams changes from the domain tables directly. Consumers then depend on the physical schema, and every column change is externally visible.
- **Outbox with CDC** has the service write purpose-built event rows to an `outbox` table within the same transaction. Debezium's **Outbox Event Router** transform captures only that table and routes each row to a topic derived from an `aggregate_type` column. The event contract is then independent of the table layout, and atomicity still holds, since the event row and the state change commit together or not at all.

Raw capture suits replication and analytics pipelines; capture plus outbox suits a deliberate integration contract published to other services.

## Pitfalls

- **A stopped connector fills the primary's disk.** The replication slot holds its confirmed LSN, and PostgreSQL retains every WAL segment after that point, so an idle or failed connector grows WAL without bound until the slot is dropped.
- **Deletes disappear under log compaction without tombstones.** If the tombstone record is filtered out — for example by an unconfigured flattening transform — the compacted topic retains the last non-null value and the key never vanishes for a rebuilt consumer.
- **Polling-based replacements silently lose deletes.** A timestamp watermark can only find rows that still exist, so a `updated_at`-driven job diverges from the source table over time.
- **Snapshot rows arrive as `op: r`, not `op: c`.** A consumer that switches only on `c`, `u` and `d` discards the entire initial state and starts from an empty projection.
- **Restart replays events already consumed.** Recovery resumes from the last confirmed LSN, so a consumer without per-key deduplication reapplies updates and re-emits downstream side effects.
- **`wal_level` below `logical` yields no usable stream.** The connector cannot decode row images from a WAL written for physical replication, and the failure appears at connector start rather than at configuration time.
