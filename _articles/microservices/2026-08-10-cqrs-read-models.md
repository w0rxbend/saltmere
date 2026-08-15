---
title: 'CQRS: when one model cannot serve both the write and the read'
date: 2026-08-10
track: microservices
summary: 'A normalized write model optimized for validation is a poor fit for complex, denormalized read views, and the two scale differently. CQRS splits them: commands change state, queries read purpose-built projections. The cost is eventual consistency between the two sides.'
reading_time: 8
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

**Gist.** A single domain model must simultaneously enforce transactional invariants on writes and answer denormalized, screen-shaped queries; the two goals push the schema in opposite directions, and the two workloads scale independently. Command Query Responsibility Segregation (CQRS) resolves the conflict by using one model to change state and a different model — often a separate store maintained by an event-driven projection — to answer queries. The cost is that the read side lags the write side: a consistency property previously supplied by the database becomes an application concern.

## One model, two jobs

The default design uses one `Order` entity for validation, mutation, joining, aggregation and pagination. That default is correct for most systems and stops being correct when the two jobs diverge.

A **write model** is normalized and behaviour-rich: it enforces invariants, guards state transitions (a cancelled order cannot be shipped), and keeps a shape that fits a single local transaction. A **read model** wants the opposite: denormalized, pre-joined, shaped exactly like the view that consumes it. Serving "customer order history with line items, shipping status and loyalty tier" from a normalized schema costs a multi-way join per request. Reshaping the schema to make that join cheap — wider tables, duplicated columns, added indexes — makes writes more expensive; keeping the schema normalized makes every read pay the join.

CQRS is the observation that one model need not serve both. Martin Fowler frames it as the notion that a different model may be used to update information than the model used to read it.

## Commands and queries have different shapes

- **Commands** change state and express intent in domain terms: `PlaceOrder`, `CancelReservation`, `RateProduct`. Microsoft's guidance is to model a command as a task ("Book hotel room") rather than a data mutation ("set `ReservationStatus` to `Reserved`"). A command is validated, may be rejected, and **succeeds or fails as a unit**.
- **Queries** return data and never mutate it. They return data-transfer objects with no domain behaviour attached — the fields the caller needs and nothing else.

The segregation comes in two strengths. The mild form keeps a single database and places separate models over it. The strong form uses **separate stores**: a store tuned for transactional writes, and one or more read stores holding denormalized views. Chris Richardson describes the read side as a view database: a read-only replica designed to support one query, kept current by subscribing to events the write side publishes.

Two consequences follow. Each side gets the schema its workload wants. And because the stores are distinct, **read and write capacity scale independently** — read replicas can be added without changing write capacity, and the reverse.

## The read side: projections

The write path handles a command by loading the aggregate, checking invariants, persisting the change, and emitting an event describing what happened (`OrderPlaced`). The read side is maintained by a **projection** — also called a materialized view or denormalizer — a consumer that subscribes to that event stream and updates a denormalized table shaped for one specific query.

The load-bearing property is that **a projection is a fold over the event stream**. Its state at any point is a function of the events consumed so far. That property is what makes a read model rebuildable: a projection that has been corrupted, or a projection added after the fact for a new view, is populated by replaying the stream from the beginning rather than by migrating data.

The second load-bearing property concerns the boundary between the two writes. The state change and the event must be committed **in the same local transaction**; otherwise the process can persist the order and fail before publishing, or publish and fail before persisting. Publishing the event from within that transaction to a table the relay reads later — the [transactional outbox](/articles/microservices/2026-07-26-transactional-outbox-pattern) — closes that hole.

### Implementation sketch (Scala)

```scala
final case class PlaceOrder(customerId: UUID, lines: List[LineItem])
final case class OrderPlaced(orderId: UUID, customerId: UUID,
                             total: BigDecimal, placedAt: Instant)

// --- WRITE SIDE ---
final class PlaceOrderHandler(orders: OrderRepo, outbox: Outbox, uow: UnitOfWork):
  def handle(cmd: PlaceOrder): Either[DomainError, UUID] =
    val order = Order.place(cmd.customerId, cmd.lines)
    order.validate.map { valid =>
      // One local transaction: a crash mid-way leaves neither the order
      // nor the outbox row durable, so the stream cannot diverge.
      uow.transact:
        orders.save(valid)
        outbox.append(OrderPlaced(valid.id, valid.customerId,
                                  valid.total, valid.placedAt))
      valid.id            // returns before any projection has run
    }

// --- READ SIDE: a fold over the stream ---
final case class CustomerSummary(orderCount: Int, lifetimeTotal: BigDecimal,
                                 lastOrderAt: Instant)

object CustomerSummaryProjection:
  // A pure fold, so a rebuild replays it. The increment is not idempotent:
  // a deployed projection commits the consumed offset with the state below.
  def apply(state: Map[UUID, CustomerSummary], e: OrderPlaced)
      : Map[UUID, CustomerSummary] =
    state.updatedWith(e.customerId):
      case Some(s) => Some(s.copy(s.orderCount + 1,
                                  s.lifetimeTotal + e.total, e.placedAt))
      case None    => Some(CustomerSummary(1, e.total, e.placedAt))

  def rebuild(stream: Iterator[OrderPlaced]): Map[UUID, CustomerSummary] =
    stream.foldLeft(Map.empty[UUID, CustomerSummary])(apply)
```

The query against such a view is a single-key lookup with no joins. The join work has moved to write time and is performed **once per event rather than once per read**, which is the reason the arrangement pays off only when reads outnumber writes. A second view — a search index over the same orders — is a second projection over the same stream, independent of the first.

## The consequence: eventual consistency

The command returns after the write commits; the projection runs afterwards, asynchronously. In the interval between those two points a query observes stale data. Microsoft's guidance names eventual consistency as the consequence of separating the stores: the read store is not updated until the change has propagated, so a query can return data that does not yet reflect the last write. Richardson lists the same cost as replication lag between the write side and the eventually consistent views.

The concrete failure mode is **read-after-write staleness**: a client submits a command, immediately re-queries the read model, the projection has not yet applied the event, and the change appears not to have happened. Mitigations, roughly in order of cost:

- **Avoid the re-read.** Return enough from the command for the caller to render the new state without querying.
- **Read the entity from the write model** and use the read model only for the aggregate views it exists to serve.
- **Track progress explicitly.** Tag the write with a version or stream offset, expose the offset the projection has consumed, and have the client wait until the projection has caught up to that offset before trusting the query.
- **Surface the lag in the interface** rather than hiding it.

CQRS converts a consistency guarantee the database provided into one the application must state and enforce.

## CQRS is not event sourcing

The two are frequently conflated. **Event sourcing** stores state as an append-only log of events and reconstructs current state by replaying it. **CQRS** is the use of different models for reads and writes. They compose well — an event-sourced write model produces exactly the stream a projection consumes — but neither requires the other. CQRS is achievable with a relational write model that emits change events through an outbox and no event store at all, and [event sourcing](/articles/microservices/2026-07-31-event-sourcing-log-as-source-of-truth) is achievable while serving reads from the rebuilt aggregate with no separate read model.

## When the pattern costs more than it returns

Fowler's caution is explicit: the pattern warrants great caution. He reports that most cases he has seen went badly, with CQRS "seen as a significant force for getting a software system into serious difficulties." The pattern adds a second store to keep synchronised, a messaging path with its own failure and duplication modes, and eventual consistency that every client must accommodate.

For an application whose read and write shapes coincide and whose load is modest, that cost buys nothing. The pattern is applied selectively, within a single bounded context, where a driver exists: command and query logic that genuinely diverge, a read/write ratio skewed heavily toward reads, or denormalized views a normalized schema cannot serve within the latency budget. Applied system-wide by default it incurs the full cost across every context while the benefit accrues to a few.

## Pitfalls

- **A projection that is not idempotent double-counts on redelivery.** At-least-once event delivery replays messages after a consumer crash between applying an event and committing its offset; an increment-style update such as `order_count + 1` is applied twice. The projection must either store the consumed offset in the same transaction as the derived state or key updates so that reapplication is a no-op.
- **Writing the state change and publishing the event as two separate operations loses events.** A crash between the database commit and the broker publish leaves a persisted order that no projection ever sees, and the read model diverges permanently. Both writes must be in one local transaction, with a relay publishing from the outbox.
- **Out-of-order delivery corrupts views that depend on ordering.** A projection maintaining "last order at" or a status field applies whichever event arrives first; a stale event arriving after a newer one overwrites the newer value. Ordering must be preserved per key, or the projection must ignore events older than the version it has already applied.
- **Read-model rebuild is only possible if the events retain the fields the view needs.** A projection added later cannot recover data the original event never carried, and the stream cannot be retrofitted.
- **A read model shared across several screens re-acquires the problem CQRS was applied to solve.** As each new view adds columns to one table, the view stops matching any single query and joins reappear on the read side.
- **The command returning success is not evidence the query will reflect it.** Tests that issue a command and immediately assert on the read model pass or fail depending on projection timing, and are a source of intermittent failures rather than a check on correctness.
