---
title: "Change Data Capture with Debezium: Your Transaction Log as an Event Stream"
date: 2026-07-31
track: microservices
summary: "Turn the Postgres WAL or MySQL binlog into an ordered stream of change events on Kafka with Debezium — log-based CDC, the before/after/op/source envelope, snapshot vs streaming, and how it relates to the outbox pattern."
reading_time: 5
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

You have a service that owns a database and needs to tell other services when data changes. The naive fix is a dual-write: update the row, then publish to Kafka. That's two systems, one non-atomic step, and eventually a message goes missing or a phantom event fires because the DB transaction rolled back. Change Data Capture (CDC) removes the second write entirely — it reads the database's own transaction log and turns every committed change into an event.

## Log-based CDC vs polling and triggers

There are three ways to detect changes. **Polling** runs `SELECT ... WHERE updated_at > ?` on a schedule: simple, but it misses deletes, adds query load, and its latency is your poll interval. **Triggers** fire on INSERT/UPDATE/DELETE and write to an audit table: accurate, but they run inside your write path and couple schema changes to trigger maintenance.

**Log-based CDC** reads the write-ahead log that the database already maintains for crash recovery and replication — the Postgres WAL or the MySQL binlog. Every committed transaction is there, in commit order, including deletes. Debezium consumes it as a replication client, so it adds no load to your application queries and captures changes with sub-second latency. The log is the source of truth the database itself trusts; CDC just reads it.

## How Debezium is deployed

Debezium runs in one of two shapes. As a **Kafka Connect** plugin it runs inside a Connect cluster and writes directly to Kafka topics — this is the mature, most common path when you already run Kafka. As **Debezium Server**, it runs standalone and sinks to other targets (Kinesis, Pulsar, Google Pub/Sub, NATS, Amazon SNS, and more) without a Connect cluster. The current release is **Debezium 3.6.Final, released July 1, 2026** (built on Apache Kafka 4.3.0), which also ships a unified Debezium CLI for pipeline management.

## Setting up the Postgres connector

Postgres needs `wal_level = logical`. Debezium uses the built-in `pgoutput` decoding plugin (no extra libraries since PG 10), a replication slot to track its position in the WAL, and a publication defining which tables are captured. A minimal Connect config:

```json
{
  "name": "inventory-connector",
  "config": {
    "connector.class": "io.debezium.connector.postgresql.PostgresConnector",
    "database.hostname": "postgres",
    "database.port": "5432",
    "database.user": "debezium",
    "database.password": "${file:/secrets:pg_password}",
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

Each captured table gets its own topic (`inventory.public.orders`). One caution: a replication slot pins WAL that Postgres cannot recycle until Debezium consumes it, so a stopped connector will grow your disk. Monitor `pg_replication_slots`.

## The change-event envelope

Every event carries a consistent envelope so consumers can react to any operation the same way:

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
- **`before` / `after`** — row state pre- and post-change. Inserts have a null `before`; deletes have a null `after` and are followed by a tombstone (null value) so log-compacted topics drop the key.
- **`source`** — provenance: connector, table, WAL position (`lsn`), transaction id, and a `snapshot` flag telling you which phase produced the event.

If downstream consumers only want the current row, the **New Record State Extraction** (ExtractNewRecordState) SMT flattens the envelope down to just the `after` state.

## Snapshot vs streaming phase

On first start the connector runs a **snapshot**: a consistent read of every included table, emitting each existing row as an `op: r` event. This gives new consumers the full current state, not just changes from now on. Once the snapshot completes, Debezium switches to the **streaming phase**, tailing the WAL from the slot's recorded position and emitting `c`/`u`/`d`/`t` events for each committed transaction. The `snapshot.mode` setting controls this — `initial` snapshots once then streams; other modes skip the snapshot or take data-free schema-only snapshots.

## Relationship to the transactional outbox

The outbox pattern solves the dual-write problem too: inside the business transaction you also insert a row into an `outbox` table, then a relay publishes those rows. CDC and outbox are complementary, and Debezium supports both.

- **Raw table CDC** streams changes to your domain tables directly. Simple, but consumers see your physical schema, and every column change leaks out.
- **Outbox with CDC** has your service write purpose-built event rows to an `outbox` table in the same transaction. Debezium's **Outbox Event Router** SMT captures only that table and routes each row to the right topic by an `aggregate_type` column. You control the event contract independently of your table layout, and you still get atomicity for free — the event and the state change commit together or not at all.

Use raw CDC for data replication and analytics pipelines; use CDC-plus-outbox when you're publishing a deliberate integration contract to other services.

**Try next:** Run `docker compose` with the official Debezium tutorial stack (Postgres + Kafka Connect), POST the config above, then `UPDATE` one row and watch the `before`/`after`/`op` envelope land on the topic with `kafka-console-consumer`.
