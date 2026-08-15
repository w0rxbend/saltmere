---
title: "The transactional outbox: eliminating the dual write"
date: 2026-07-26
track: microservices
summary: "A database commit and a broker publish are two separate systems with no shared transaction. The outbox pattern restores atomicity by writing the event to a table in the same local transaction, then relaying that table to the broker."
reading_time: 6
tags: [outbox, events, kafka, debezium, cdc, reliability, messaging]
sources:
  - title: "Chris Richardson, Microservices Pattern: Transactional outbox"
    url: "https://microservices.io/patterns/data/transactional-outbox.html"
  - title: "Debezium Documentation: Outbox Event Router"
    url: "https://debezium.io/documentation/reference/stable/transformations/outbox-event-router.html"
  - title: "Oskar Dudycz, Outbox, Inbox patterns and delivery guarantees explained"
    url: "https://event-driven.io/en/outbox_inbox_patterns_and_delivery_guarantees_explained/"
  - title: "AWS Prescriptive Guidance: Transactional outbox pattern"
    url: "https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html"
---

**Gist.** A service that commits a row to its database and then publishes an event to a message broker performs two independent commits, and a crash between them leaves the two systems disagreeing. The transactional outbox collapses the pair into one commit by inserting the event into an **outbox table inside the same local database transaction**, after which a separate relay process moves rows from that table to the broker. The cost is that the relay can publish a row and then fail before recording that it did, so delivery becomes **at-least-once** and every consumer must tolerate duplicates.

## The dual-write problem

Consider a service that saves an order to PostgreSQL and then publishes `OrderPlaced` to Kafka. Two systems, two commits, no shared transaction. Both orderings of the two calls leave a failure window:

- The database commit succeeds and the broker publish fails (network partition, broker unavailable). The order exists; no downstream consumer ever learns of it.
- The publish succeeds and the database transaction rolls back. An event exists describing an order that does not.

Wrapping the two in a single distributed transaction is not available in most stacks: message brokers generally do not implement the XA two-phase commit (2PC) interface, and where a 2PC coordinator is available it holds locks across the network and couples the availability of the database to the availability of the broker. The outbox addresses the narrower problem — **getting one event out of one transaction reliably** — and not the coordination of writes across several services, which is what sagas address.

## Restoring atomicity: write the event where the data lives

Rather than publishing directly, the service inserts the event as a row in an outbox table within the same transaction as the business write. The invariant is the one the database already enforces: **either both rows are durable or neither is**. There is no interval in which the order exists without its pending event.

```sql
CREATE TABLE outbox (
    id              UUID PRIMARY KEY,
    aggregatetype   VARCHAR(255) NOT NULL,   -- e.g. 'Order'
    aggregateid     VARCHAR(255) NOT NULL,   -- e.g. the order id
    type            VARCHAR(255) NOT NULL,   -- e.g. 'OrderPlaced'
    payload         JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    published       BOOLEAN NOT NULL DEFAULT false  -- polling relay only; CDC ignores it
);

BEGIN;
  INSERT INTO orders (id, customer_id, total, status)
  VALUES ('ord_123', 'cust_9', 4200, 'PLACED');

  INSERT INTO outbox (id, aggregatetype, aggregateid, type, payload)
  VALUES (gen_random_uuid(), 'Order', 'ord_123', 'OrderPlaced',
          '{"orderId":"ord_123","total":4200}');
COMMIT;
```

The column names are not arbitrary: `aggregatetype`, `aggregateid`, `type` and `payload` are the field names Debezium's outbox router expects by default, which matters for the relay described next.

## The relay: draining the table onto the broker

Once committed, the outbox table is the authoritative record of what remains to be published. A separate process drains it. Two approaches are established.

**Polling publisher.** A background job repeatedly issues `SELECT * FROM outbox WHERE published = false ORDER BY created_at LIMIT 100`, publishes each row, then marks it published or deletes it. It requires no additional infrastructure, but it introduces **latency bounded below by the poll interval**, issues queries against the primary whether or not work exists, and performs one extra write per event to record completion.

**Change data capture (CDC).** A connector tails the database's transaction log — the write-ahead log (WAL) in PostgreSQL, the binary log (binlog) in MySQL — and emits each committed insert as it appears. No polling query touches the primary. Debezium ships an **Outbox Event Router**, a Kafka Connect single message transform (SMT) that reshapes a log-tailed outbox row into a domain event: `aggregatetype` selects the destination topic (by default `outbox.event.` followed by the field's value, so `outbox.event.Order`), `aggregateid` becomes the Kafka record key, and `payload` becomes the message value. Under Kafka's default partitioner the key determines the partition, so all events sharing an `aggregateid` land together.

```properties
# Kafka Connect / Debezium connector config
transforms=outbox
transforms.outbox.type=io.debezium.transforms.outbox.EventRouter
transforms.outbox.table.expand.json.payload=true
value.converter=org.apache.kafka.connect.json.JsonConverter
```

| | Polling publisher | CDC (Debezium) |
|---|---|---|
| Extra infrastructure | None | Kafka Connect + Debezium |
| Latency | Bounded by poll interval | Bounded by log-tailing lag |
| Load on primary | Repeated queries plus status updates | Log tailing only |
| Ordering | Insertion order, via `ORDER BY` and a single worker | Log order, preserved per record key |
| Operational surface | One job | Connector liveness and offset tracking |

## Delivery semantics and the state machine

Each row passes through two observable transitions: committed to the outbox, then published to the broker and recorded as such. The second transition is not atomic — the broker acknowledgement and the record of it are separate writes to separate systems. A polling publisher can send the record and crash before the status update; a CDC connector can emit the record and crash before committing its log offset. In both cases recovery re-reads a row that has already reached the broker. The pattern therefore delivers **at-least-once: never zero, occasionally more than one**. No arrangement of relay-side logic removes the duplicate, because the acknowledgement and the effect live in different systems — the same problem the outbox solved one layer down.

Duplicate suppression consequently belongs to the consumer. Two mechanisms are used: recording processed event identifiers in a table of the consumer's own (an **inbox**) with a unique constraint that rejects the second arrival, or making the business operation naturally idempotent, for example an upsert keyed by order identifier rather than an increment of a stock counter.

## Ordering

Within one aggregate, ordering follows from the mechanism, with one caveat about the polling relay. A single-threaded polling publisher drains rows in whatever order its `ORDER BY` imposes, and `created_at` is stamped when the row is inserted, not when its transaction commits: two concurrent transactions can commit in the opposite order to their inserts, and rows sharing a timestamp have no defined relative order at all. A monotonically increasing sequence column, or restricting the outbox to one writer per aggregate, is what makes the drain order meaningful. The CDC relay has no such gap, because the transaction log records commits in commit order, and the router's use of `aggregateid` as the record key keeps every event for one order on the same partition, where the broker preserves offset order. Ordering **across** aggregates is not guaranteed by either relay. A consumer that requires a specific interleaving of `OrderPlaced` and `CustomerUpdated` is expressing a modelling requirement the relay cannot satisfy.

### Implementation sketch (Scala)

The load-bearing property is that the event insert shares the caller's connection and transaction. The sketch below uses plain JDBC to make the shared transaction explicit; nothing here handles retries or connection pooling.

```scala
final case class OutboxEvent(
    aggregateType: String,
    aggregateId: String,
    eventType: String,
    payload: String
)

def insertOutbox(conn: java.sql.Connection, e: OutboxEvent): Unit =
  val ps = conn.prepareStatement(
    """INSERT INTO outbox (id, aggregatetype, aggregateid, type, payload)
       VALUES (gen_random_uuid(), ?, ?, ?, ?::jsonb)"""
  )
  ps.setString(1, e.aggregateType)
  ps.setString(2, e.aggregateId)
  ps.setString(3, e.eventType)
  ps.setString(4, e.payload)
  ps.executeUpdate()

/** Business write and event insert commit together or not at all. */
def placeOrder(conn: java.sql.Connection, orderId: String, total: Long): Unit =
  conn.setAutoCommit(false)
  try
    val ps = conn.prepareStatement(
      "INSERT INTO orders (id, total, status) VALUES (?, ?, 'PLACED')"
    )
    ps.setString(1, orderId)
    ps.setLong(2, total)
    ps.executeUpdate()

    insertOutbox(conn, OutboxEvent(
      "Order", orderId, "OrderPlaced", s"""{"orderId":"$orderId","total":$total}"""
    ))
    conn.commit()
  catch
    case e: Throwable => conn.rollback(); throw e
```

A consumer-side inbox is the mirror image: an `INSERT` of the event identifier under a unique constraint, executed in the same transaction as the effect, so a duplicate arrival fails the insert and the effect rolls back with it.

## Scope

The outbox makes writes reliable for one service against one database. It does not coordinate a sequence of writes across services; sagas do that, and the two compose — each saga step publishes its completion event through its own local outbox, so the workflow's progress does not depend on a synchronous broker call succeeding at the moment of commit.

## Pitfalls

- **The event insert runs on a different connection than the business write.** The two commits are then independent again and the dual write has been reproduced inside the service, usually invisibly, because a connection pool hands out a second connection without complaint.
- **The consumer is not idempotent.** A relay restart between publish and acknowledgement replays the event, and a non-idempotent handler double-charges, double-ships, or double-increments.
- **Multiple polling publishers run concurrently without row locking.** Two workers select the same unpublished batch and publish it twice; ordering within an aggregate is also lost, since the guarantee rests on a single drainer.
- **Published rows are never removed.** The outbox table grows without bound, and the polling query's scan cost grows with it.
- **Ordering across aggregates is assumed.** Two different `aggregateid` values may hash to different partitions, and a consumer reading several partitions sees no defined interleaving between them regardless of commit order.
- **The connector is unmonitored.** A stopped Debezium connector produces no errors at the writing service — commits keep succeeding — while the outbox silently accumulates undelivered rows.
- **The payload embeds internal table columns.** The outbox row becomes a published schema, and a later column rename in the business table breaks consumers that were never intended to see it.
