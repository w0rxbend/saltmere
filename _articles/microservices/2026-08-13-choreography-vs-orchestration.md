---
title: "Choreography vs orchestration: who owns the workflow?"
date: 2026-08-13
track: microservices
summary: "Choreography lets services react to each other's events with no central brain; orchestration gives one component the workflow and lets it command the rest. The trade is coupling vs visibility — and the pragmatic answer is hybrid: orchestrate within a bounded context, choreograph between them."
reading_time: 5
tags: [choreography, orchestration, sagas, event-driven, workflow]
sources:
  - title: "Chris Richardson — Pattern: Saga (microservices.io)"
    url: "https://microservices.io/patterns/data/saga.html"
  - title: "Chris Richardson — Coordinating sagas (part 2)"
    url: "https://microservices.io/post/sagas/2019/08/04/developing-sagas-part-2.html"
  - title: "Yan Cui — Choreography vs Orchestration in the land of serverless"
    url: "https://theburningmonk.com/2020/08/choreography-vs-orchestration-in-the-land-of-serverless/"
  - title: "Azure Architecture Center — Choreography pattern"
    url: "https://learn.microsoft.com/en-us/azure/architecture/patterns/choreography"
---

A multi-service business flow needs coordination. There are exactly two places the coordination logic can live: **nowhere in particular** (choreography) or **somewhere specific** (orchestration).

- **Choreography:** each service publishes *events* — facts about what happened — and other services subscribe and react. Nobody owns the flow; it *emerges* from subscriptions.
- **Orchestration:** one component owns the flow and sends *commands* — imperatives — to participants, collects replies, and decides the next step.

The event/command distinction is the tell. An event (`OrderPlaced`) is a past-tense fact that doesn't know or care who listens. A command (`ReserveInventory`) is an imperative that names its target and expects an answer. If your "events" name the receiver and demand action, you have built an orchestrator and called it choreography — a common design-review catch.

## One flow, modeled both ways

Order placement: charge payment, reserve inventory, arrange shipping.

**Choreographed** — the flow is the sum of subscriptions:

```
Order    --OrderPlaced-->        (topic)
Payment  hears OrderPlaced   --> charges     --PaymentCompleted-->
Inventory hears PaymentCompleted --> reserves --InventoryReserved-->
Shipping hears InventoryReserved --> books    --ShipmentBooked-->
Order    hears ShipmentBooked --> marks order confirmed

Events:   OrderPlaced, PaymentCompleted, PaymentFailed,
          InventoryReserved, OutOfStock, ShipmentBooked
Failure:  Inventory hears PaymentCompleted, has no stock
          --> emits OutOfStock
          Payment hears OutOfStock --> refunds (compensation)
          Order hears OutOfStock  --> marks order rejected
```

**Orchestrated** — an order orchestrator owns the sequence:

```
Orchestrator state machine for order #42:
  1. send ChargePayment(payment-svc)      await PaymentResult
  2. send ReserveInventory(inventory-svc) await ReservationResult
  3. send BookShipment(shipping-svc)      await BookingResult
  4. mark order confirmed

Commands: ChargePayment, ReserveInventory, BookShipment,
          RefundPayment, ReleaseInventory   (compensations)
Failure:  step 2 returns OutOfStock
          --> orchestrator runs compensation: RefundPayment
          --> marks order rejected
```

Same business flow, same failure case — but look where the failure logic sits. In choreography it's smeared across Payment and Order (each must know what `OutOfStock` means *for them*). In orchestration it's three consecutive lines in one state machine.

## The actual trade-offs

**Visibility.** With an orchestrator, "where is order #42 stuck?" is a state lookup. With choreography it's a distributed-tracing archaeology dig: the workflow exists only in your head and in ten services' subscription lists. Azure's own write-up of the choreography pattern flags exactly this — the business flow becomes hard to monitor because no component knows it.

**Failure handling.** Orchestrators centralize timeouts, retries, and compensation ordering. In choreography, every service must handle every failure event that concerns it, and *compensations run in reverse order of a sequence no one wrote down*. Adding "cancel order" to the choreographed version touches four services; in the orchestrated one it's a new branch in the state machine.

**Coupling.** Choreography wins here. Payment doesn't know Shipping exists. New consumers (fraud scoring, analytics, email) attach to `OrderPlaced` without anyone's permission. An orchestrator, by contrast, knows every participant's API and becomes a change bottleneck — and, done badly, a "distributed monolith brain" that reduces services to dumb CRUD endpoints.

**Availability.** The orchestrator is one more thing that can be down mid-flow. This is exactly the problem durable-execution engines solve — [Temporal-style event-sourced workflow state](/articles/microservices/2026-07-31-temporal-durable-execution/) makes the orchestrator crash-proof rather than a single point of failure.

## Mapping to sagas

Both are coordination styles for the same underlying pattern: a [saga](/articles/microservices/2026-07-24-sagas-over-two-phase-commit/) — a sequence of local transactions with compensations instead of a distributed lock. Richardson names the two variants directly: *choreography-based sagas* (events trigger the next local transaction) and *orchestration-based sagas* (a saga orchestrator issues commands). His rule of thumb: choreography is fine for simple sagas with few steps; once the saga has real branching and compensation, orchestrate. Either way, every step still needs reliable event publication — the [transactional outbox](/articles/microservices/2026-07-26-transactional-outbox-pattern/) applies to both styles.

## The hybrid reality

The clean interview answer is Yan Cui's: **orchestrate within a bounded context, choreograph between bounded contexts.** Inside "order fulfillment," the steps are one team's business logic — make the workflow explicit in an orchestrator you can read, test, and monitor. Between contexts — fulfillment, notifications, analytics, fraud — publish domain events and let other teams subscribe, because a central orchestrator spanning team boundaries couples their release cycles to yours.

## Anti-pattern: pinball architecture

The failure mode to name is the **implicit workflow**: a ten-step business process expressed as ten consumers each reacting to the previous event — the request ricochets through the system like a pinball. Symptoms: nobody can draw the flow; onboarding requires reading every consumer; a "simple" reorder of two steps takes a multi-team change; and a stuck flow produces no error anywhere, just silence. If you find yourself *simulating* an orchestrator by passing growing "process state" inside events, the workflow wants to be explicit — write the state machine.

**Try next:** pick one multi-service flow you own and try to draw it end-to-end from the code alone, failure paths included. If you can't, that flow is choreographed by accident, not by choice — write the sequence down and decide deliberately which half deserves an orchestrator.
