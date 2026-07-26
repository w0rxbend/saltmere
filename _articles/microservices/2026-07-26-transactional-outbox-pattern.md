---
title: "The transactional outbox: never lose an event to a dual write again"
date: 2026-07-26
track: microservices
summary: "Writing to your database and publishing an event are two separate systems that can't share a commit. The outbox pattern makes them atomic anyway — write the event to a table in the same local transaction, then relay it."
reading_time: 5
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

## The dual-write problem

`OrderService` saves an order to Postgres, then publishes `OrderPlaced` to Kafka. Two systems, two commits, no shared transaction. Every ordering of those two calls has a failure window:

- DB commit succeeds, broker publish fails (network blip, broker down) → the order exists but nobody downstream ever hears about it.
- Publish succeeds, DB commit fails or rolls back → an event for an order that doesn't exist.

You can't wrap a relational database and a message broker in a single two-phase commit in any practical stack — brokers generally don't speak XA, and even where they do, 2PC's locking cost and availability coupling make it a bad trade at microservice scale (the same reason sagas replace 2PC for cross-service workflows — see the companion article on sagas for that side of the coin). The outbox pattern solves the *narrower* problem: reliably getting one event out of one transaction, not coordinating multiple services.

## The fix: write the event where the data lives

Instead of publishing directly, insert the event into an **outbox table** in the same local transaction as the business write. Either both rows commit or neither does — that's a guarantee your database already gives you for free.

```sql
CREATE TABLE outbox (
    id              UUID PRIMARY KEY,
    aggregatetype   VARCHAR(255) NOT NULL,   -- e.g. 'Order'
    aggregateid     VARCHAR(255) NOT NULL,   -- e.g. the order id
    type            VARCHAR(255) NOT NULL,   -- e.g. 'OrderPlaced'
    payload         JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

BEGIN;
  INSERT INTO orders (id, customer_id, total, status)
  VALUES ('ord_123', 'cust_9', 4200, 'PLACED');

  INSERT INTO outbox (id, aggregatetype, aggregateid, type, payload)
  VALUES (gen_random_uuid(), 'Order', 'ord_123', 'OrderPlaced',
          '{"orderId":"ord_123","total":4200}');
COMMIT;
```

That column shape — `aggregatetype` / `aggregateid` / `type` / `payload` — isn't arbitrary; it's the default schema Debezium's outbox router expects, which matters for the next step.

## The relay: getting rows out of the table and onto the broker

The outbox table is now the source of truth for "what needs publishing." Something has to drain it. Two established approaches:

**Polling publisher.** A background job periodically does `SELECT * FROM outbox WHERE published = false ORDER BY created_at LIMIT 100`, publishes each row, then marks it published (or deletes it). Simple, no new infrastructure, but it adds polling latency and doesn't scale well past a moderate write rate — plus you're doing an extra write per row to mark it done.

**Change data capture (CDC).** A connector tails the database's transaction log (WAL in Postgres, binlog in MySQL) and streams row inserts as they commit — no polling, lower latency, and it doesn't add load to the primary via repeated queries. Debezium is the reference implementation here, and it ships a purpose-built **Outbox Event Router** single message transform (SMT) that reads the log-tailed outbox row and reshapes it into a proper domain event: `aggregatetype` picks the target topic (`outbox.event.Order`), `aggregateid` becomes the Kafka partition key (so all events for one order stay ordered), and `payload` becomes the message body.

```properties
# Kafka Connect / Debezium connector config
transforms=outbox
transforms.outbox.type=io.debezium.transforms.outbox.EventRouter
transforms.outbox.table.expand.json.payload=true
value.converter=org.apache.kafka.connect.json.JsonConverter
```

| | Polling publisher | CDC (Debezium) |
|---|---|---|
| Extra infra | None | Kafka Connect + Debezium |
| Latency | Poll interval (seconds) | Near real-time |
| DB load | Repeated polling queries + status updates | Log tailing, negligible |
| Ordering | Easy (ORDER BY + single worker) | Preserved per partition key |
| Failure mode | Simpler to reason about | Needs connector monitoring/offset tracking |

Start with polling if you're not already running Kafka; move to CDC once polling latency or DB load becomes a real cost.

## At-least-once delivery means idempotent consumers, always

Whichever relay you pick, it can crash *after* publishing but *before* marking the row done (polling) or *after* the connector commits its offset but the consumer hasn't finished (CDC). Both retry, so both give you **at-least-once** delivery — never zero, occasionally more than one. There's no way to get exactly-once out of this without the consumer's help, so the consumer must be idempotent: track processed event ids in its own table (an "inbox") and skip duplicates with a unique constraint, or make the business operation naturally idempotent (upsert by order id rather than "increment stock by N").

## Ordering

Within a single aggregate, ordering falls out for free: the outbox rows are inserted in commit order, a polling publisher drains them in that order, and Debezium's router uses `aggregateid` as the partition key so a topic-partitioned broker like Kafka keeps all events for one order in sequence. Ordering *across* aggregates is not guaranteed and usually shouldn't be relied on — if service B needs to know order matters between an `OrderPlaced` and a `CustomerUpdated`, that's a modeling problem, not something the relay should paper over.

## Where it stops helping

The outbox only makes single-service, single-database writes reliable. It says nothing about coordinating a *sequence* of writes across services — that's what sagas are for, and the two combine naturally: each saga step publishes its "step completed" event via its own local outbox, so the workflow's reliability doesn't depend on that step's broker call succeeding synchronously.

**Try next:** add an outbox table to one service that currently publishes directly after a DB commit, kill the process between the commit and the publish call, and confirm you've reproduced the dual-write bug — then swap in the outbox and prove the event survives the crash on restart.
