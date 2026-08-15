---
title: 'CQRS: when one model can''t serve both the write and the read'
date: 2026-08-10
track: microservices
summary: 'A normalized write model optimized for validation is a poor fit for complex, denormalized read views — and the two scale differently. CQRS splits them: commands change state, queries read purpose-built projections. The catch you must name in an interview is eventual consistency.'
reading_time: 6
tags:
- cqrs
- read-models
- projections
- eventual-consistency
- event-sourcing
- ddd
- scalability
- event-driven
- bounded-context
sources:
- title: 'Martin Fowler, bliki: CQRS'
  url: https://martinfowler.com/bliki/CQRS.html
- title: 'Microsoft Azure Architecture Center: CQRS pattern'
  url: https://learn.microsoft.com/en-us/azure/architecture/patterns/cqrs
- title: 'Chris Richardson, Microservices Pattern: CQRS'
  url: https://microservices.io/patterns/data/cqrs.html
- title: Greg Young, CQRS Documents
  url: https://cqrs.wordpress.com/wp-content/uploads/2010/11/cqrs_documents.pdf
- title: Greg Young — CQRS (what it is and isn't)
  url: https://gregfyoung.wordpress.com/2012/03/02/cqrs/
- title: Udi Dahan — Clarified CQRS
  url: https://udidahan.com/2009/12/09/clarified-cqrs/
---

## One model, two jobs, both done badly

Most systems start with a single model that serves everything. The same `Order` entity that you validate and mutate is the same one you join, aggregate, and paginate to render a dashboard. For a while this is fine — it's the default, and the default is usually right.

It stops being fine when the two jobs pull in opposite directions. A **write model** wants to be normalized and behavior-rich: it enforces invariants, guards transitions ("you can't ship a cancelled order"), and keeps a clean transactional shape. A **read model** wants the opposite: denormalized, pre-joined, shaped exactly like the screen that consumes it. Serving a "customer order history with line items, shipping status, and loyalty tier" from a normalized schema means five joins and a query planner you're now fighting. Optimize the schema for that read and your writes get awkward; optimize for writes and every read pays a join tax.

CQRS — **Command Query Responsibility Segregation** — is the observation that you don't have to pick. Martin Fowler frames it as "the notion that you can use a different model to update information than the model you use to read information." Commands go through one model; queries come from another.

## Commands and queries are different shapes

Split your operations cleanly:

- **Commands** change state and express intent in domain terms — `PlaceOrder`, `CancelReservation`, `RateProduct`. Microsoft's guidance is worth internalizing: model commands as tasks ("Book hotel room"), not as data mutations ("set `ReservationStatus` to `Reserved`"). A command is validated, may be rejected, and either succeeds or fails as a unit.
- **Queries** return data and never mutate it. They hand back DTOs or view objects with no domain behavior attached — just the fields the caller needs.

This is a segregation of *responsibility*, and it comes in two strengths. The mild version keeps one database but uses separate models on top of it. The strong version — the one worth discussing at scale — uses **separate stores**: a relational store optimized for transactional writes, and one or more read stores (often a document or search store) holding denormalized views. Chris Richardson describes the read side as "a view database, which is a read-only 'replica' designed specifically to support that query," kept current by subscribing to events the write side publishes.

Two independent benefits fall out. First, each side gets a schema it actually wants. Second — and this is often the real driver — read and write scale independently. Most systems read far more than they write; now you can replicate and fan out the read stores without touching write capacity, and vice versa.

## How the read side is built: projections

The write side handles a command: load the aggregate, check invariants, persist the change, and emit an event describing what happened (`OrderPlaced`). The read side is maintained by a **projection** (also called a materialized view or denormalizer) — a consumer that subscribes to those events and updates a denormalized read table shaped for a specific query.

Here's a command handler plus an async projection that maintains a per-customer order-summary read model:

```python
# --- WRITE SIDE: command handler ---
class PlaceOrderHandler:
    def handle(self, cmd: PlaceOrder) -> None:
        # 1. Load aggregate, enforce invariants
        customer = self.customers.get(cmd.customer_id)
        if not customer.can_order():
            raise DomainError("customer not eligible to order")

        order = Order.place(cmd.customer_id, cmd.line_items)

        # 2. Persist state change AND the event atomically.
        #    Same local transaction -> no dual-write hole (see: outbox).
        with self.uow.begin():
            self.orders.save(order)
            self.outbox.append(OrderPlaced(
                order_id=order.id,
                customer_id=order.customer_id,
                total=order.total,
                item_count=len(order.line_items),
                placed_at=order.placed_at,
            ))
        # Command returns now. The read model is NOT updated yet.


# --- READ SIDE: async projection ---
class CustomerOrderSummaryProjection:
    """Subscribes to the event stream, maintains a denormalized read table.
    Runs independently of the write path."""

    def on_order_placed(self, e: OrderPlaced) -> None:
        # Upsert a row shaped exactly like the 'order history' screen.
        # No joins at query time — the view is pre-computed.
        self.read_db.execute("""
            INSERT INTO customer_order_summary
                (customer_id, order_count, lifetime_total, last_order_at)
            VALUES (%(cid)s, 1, %(total)s, %(at)s)
            ON CONFLICT (customer_id) DO UPDATE SET
                order_count    = customer_order_summary.order_count + 1,
                lifetime_total = customer_order_summary.lifetime_total + %(total)s,
                last_order_at  = %(at)s
        """, {"cid": e.customer_id, "total": e.total, "at": e.placed_at})


# --- QUERY SIDE: trivial, no domain logic ---
def get_customer_summary(read_db, customer_id) -> dict:
    return read_db.fetch_one(
        "SELECT * FROM customer_order_summary WHERE customer_id = %s",
        customer_id)
```

Notice the query is a single-row lookup with zero joins. All the work moved to write time, done once per event instead of once per read. If you need a second view — say, a search index over orders — you add another projection reading the same stream. Because a projection is just a fold over events, you can rebuild a broken or newly-added read model by replaying the stream from the start.

## The consequence you must name: eventual consistency

Look again at the handler comment: *"the read model is NOT updated yet."* The command returns after the write commits; the projection runs afterward, asynchronously. Between those two moments, a query hits stale data. Microsoft states it plainly — with separate stores, "read stores may lag behind writes." Richardson calls it "replication lag / eventually consistent views."

In an interview, say this out loud and then say how you handle it. The classic trap is **read-your-writes**: a user submits a form, the UI immediately re-queries, the projection hasn't caught up, and their change appears to vanish. Mitigations, roughly in order of preference:

- **Don't re-read.** Have the command return enough for the UI to update optimistically, or echo the command's result.
- **Read from the write model** for the just-written entity, using the read model only for the broader views.
- **Version / track progress.** Tag writes with a version or event offset and have the client wait until the read model has caught up to it before trusting a query.
- **Show it in the UX.** "Processing…" is honest and cheap.

Whatever you choose, the point is that CQRS turns a consistency question you got for free into one you now own.

## CQRS is not event sourcing

These two get conflated constantly; keep them separate. **Event sourcing** stores state as an append-only log of events and rebuilds current state by replaying them. **CQRS** is about using different models for reads and writes. They're complementary — an event-sourced write model produces exactly the event stream a projection wants to consume, which is why they're so often shown together — but each stands alone. You can do CQRS with a plain relational write model that emits change events (or uses a [transactional outbox](/articles/microservices/2026-07-26-transactional-outbox-pattern) to publish them reliably), no event store in sight. And you can do [event sourcing](/articles/microservices/2026-07-31-event-sourcing-log-as-source-of-truth) while reading straight from a rebuilt aggregate, no separate read model at all. Adopt them independently.

## When it's overkill

Fowler's caution is the most important sentence in his write-up: "you should be very cautious about using CQRS." He's blunt that most cases he's seen went badly, with CQRS "seen as a significant force for getting a software system into serious difficulties." It adds complexity, extra stores to keep in sync, messaging with its failures and duplicates, and eventual consistency you have to design around.

So for the typical CRUD app — where the read and write shapes are basically the same and load is modest — CQRS is a net loss. Reach for it selectively, at a single bounded context, when you have a real driver: a genuinely complex domain where command and query logic diverge, a punishing read/write ratio, or expensive denormalized views that a normalized schema can't serve. Apply it to the whole system by default and you've bought all the cost for little of the benefit.

**Try next:** take one read-heavy endpoint in a service you know, sketch the denormalized view it really wants, and write the single projection that would maintain it from that service's events — then decide honestly whether the eventual consistency is worth it.
