---
title: "CQRS: one model to change, another to read"
date: 2026-08-11
track: microservices
summary: "The shape that makes writes correct is rarely the shape that makes reads fast. CQRS splits them: commands mutate through a normalized write model, queries hit a separate denormalized read model kept in sync by events. Powerful per bounded context — and a liability if you reach for it as a top-level architecture."
reading_time: 6
tags: [cqrs, read-models, projections, eventual-consistency, event-driven, bounded-context]
sources:
  - title: "Martin Fowler — CQRS"
    url: "https://martinfowler.com/bliki/CQRS.html"
  - title: "Greg Young — CQRS (what it is and isn't)"
    url: "https://gregfyoung.wordpress.com/2012/03/02/cqrs/"
  - title: "Udi Dahan — Clarified CQRS"
    url: "https://udidahan.com/2009/12/09/clarified-cqrs/"
  - title: "Microsoft Azure Architecture Center — CQRS pattern"
    url: "https://learn.microsoft.com/en-us/azure/architecture/patterns/cqrs"
  - title: "Chris Richardson — Pattern: CQRS (microservices.io)"
    url: "https://microservices.io/patterns/data/cqrs.html"
---

A normalized schema is designed to keep writes correct: foreign keys, constraints, one fact in one place, no update anomalies. A query screen is designed to answer a question fast: everything a page needs in one row, pre-joined, pre-aggregated, no thinking at read time. These two goals pull in opposite directions. On a single model you serve reads with six-table joins and a `GROUP BY`, or you denormalize and fight write anomalies. **CQRS — Command Query Responsibility Segregation — stops choosing.** You keep two models: commands flow through the write model, queries read from a separate model shaped for exactly the queries you run.

## The core idea

Split the round trip in two:

- **Commands** express intent and mutate state — `PlaceOrder`, `RateProduct`, `CancelReservation`. They run through the write model, which owns the domain rules and transactional integrity. A command returns success or failure, not data.
- **Queries** return data and change nothing. They read from a **read model**: a denormalized store (a flat table, a document, a search index) whose columns match a screen, not an entity.

The two are kept in sync by the write side **emitting events** that a **projector** consumes to update the read store. Note what CQRS is *not*: it is not event sourcing, and it does not require it. As Greg Young puts it, CQRS is "a small tactical pattern" — you can apply it over a perfectly ordinary CRUD write model. Event sourcing is one way to feed the read side; it is not a prerequisite. The two pair well and are frequently seen together, but they are independent decisions.

## A concrete example

An orders service. The write side owns a normalized `orders` / `order_lines` schema and, on each state change, publishes an event. The read side maintains a single `order_summary` table built for the "my orders" list — no joins at query time.

```
Write model (normalized)          Read model (denormalized)
  orders(id, customer_id, ...)      order_summary(
  order_lines(order_id, sku, qty)     order_id, customer_id, status,
                                       item_count, total_cents, placed_at,
                                       updated_at)
```

The projector consumes events and upserts the summary row:

```python
def project(event, read_db):
    if event.type == "OrderPlaced":
        read_db.upsert("order_summary", {
            "order_id":   event.order_id,
            "customer_id": event.customer_id,
            "status":     "placed",
            "item_count": sum(l["qty"] for l in event.lines),
            "total_cents": event.total_cents,
            "placed_at":  event.at,
            "updated_at": event.at,
        })
    elif event.type == "OrderShipped":
        read_db.update("order_summary", event.order_id,
                       status="shipped", updated_at=event.at)
```

The query is then trivial — and fast, because the work was done at write time:

```sql
SELECT order_id, status, item_count, total_cents, placed_at
FROM   order_summary
WHERE  customer_id = $1
ORDER  BY placed_at DESC;
```

How does the event actually reach the projector reliably? That is not a CQRS concern — it is the same delivery problem every event-driven system has. Use the [transactional outbox](/articles/microservices/2026-07-26-transactional-outbox-pattern/) (or [Debezium CDC](/articles/microservices/2026-07-31-debezium-change-data-capture/)) so the event is written atomically with the state change, and make the projector **idempotent**, since your broker delivers at-least-once — see [idempotency keys](/articles/microservices/2026-07-30-idempotency-keys-safe-retries/). If the write model is already [event-sourced](/articles/microservices/2026-07-31-event-sourcing-log-as-source-of-truth/), the event log *is* the source, and the projector is just another subscriber replaying the stream.

## Eventual consistency and read-your-writes

The read model updates *after* the command commits, so there is a window — milliseconds usually, seconds under load — where a query returns stale data. A user places an order, the list still shows the old set, and they conclude it failed. You cannot make this window zero without collapsing back to one model, so design the UI around it:

- **Optimistic UI:** the command handler returns the new state; the client renders it immediately from the response rather than re-querying the lagging read model.
- **Read-your-writes routing:** after a write, route that user's next reads to the write model (or a synchronously-updated cache) until the projection catches up.
- **Show the seam honestly:** a "processing" state beats a screen that silently contradicts what the user just did.

| | Write model | Read model |
|---|---|---|
| Shape | Normalized, entity-centric | Denormalized, screen-centric |
| Optimized for | Correctness, invariants | Query speed |
| Consistency | Strong (transactional) | Eventual (via events) |
| Scaling | Modest | Replicate/scale reads freely |

## When *not* to use it

This is where CQRS earns its bad reputation. Fowler is blunt: it is "a significant mental leap" and "the majority of the cases I've run into" applied it where it didn't belong, becoming "a significant force for getting a software system into serious difficulties." The critical constraint, from both Fowler and Greg Young: **CQRS is not a top-level architecture.** Apply it *inside a single bounded context* where the read and write shapes genuinely diverge — never as the default for every service. Young: "CQRS is not a top level architecture." Udi Dahan's "Clarified CQRS" makes the same point from the other side: much of the complexity people blame on CQRS actually comes from bolting on collaboration, messaging, and eventual consistency where the domain never needed them.

So the honest test: does this bounded context have collaborative writes, complex invariants, or a read/write scaling mismatch that a single model serves badly? Then split. Is it a form that edits a row and shows the row back? Then plain CRUD wins — two models, a projector, and an eventually-consistent read store are pure cost with no payoff. The trade is real on both sides: you buy read scalability and dead-simple queries with added moving parts, staleness, and the operational burden of keeping a projection healthy and rebuildable.

**Try next:** pick one read-heavy query in an existing service that already does an ugly multi-table join. Stand up a single denormalized `*_summary` table, write a projector that upserts it from the events the write side already emits (make it idempotent on event id), and point just that one query at it. Measure the latency drop — then decide whether the staleness is acceptable before you split anything else.
