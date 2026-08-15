---
title: "Choreography vs orchestration: who owns the workflow?"
date: 2026-08-13
track: microservices
summary: "Choreography lets services react to each other's events with no central component; orchestration gives one component the workflow and lets it command the rest. The trade is coupling against visibility, and the common compromise is hybrid: orchestrate within a bounded context, choreograph between them."
reading_time: 7
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

**Gist.** A business flow that spans several services needs its steps sequenced and its failures compensated, and that coordination logic has to be located somewhere. Choreography distributes it: each service publishes events and each interested service subscribes, so the flow exists only as the sum of subscriptions. Orchestration concentrates it in one component that issues commands and tracks progress, which buys a queryable workflow state at the price of a component that knows every participant's interface and must itself stay available.

## The distinction is event versus command

The two styles differ in the kind of message that crosses the wire.

- **Choreography:** a service publishes an *event* — a past-tense fact, such as `OrderPlaced` — that does not name a recipient. Other services subscribe and react. No component owns the flow.
- **Orchestration:** one component owns the flow and sends *commands* — imperatives such as `ReserveInventory` — to named participants, collects replies, and decides the next step.

**The message shape is the reliable test, not the transport.** An event carries no addressee and no expectation of a reply; a command names its target and expects one. A system whose "events" name the receiver and demand an action is an orchestration with event-shaped naming, and the coupling it was meant to avoid is still present.

## One flow, modelled both ways

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

The business flow and the failure case are identical; the location of the failure logic is not. **In the choreographed version the meaning of `OutOfStock` is encoded separately in Payment and in Order, each of which must interpret the event for itself. In the orchestrated version it is a single branch in one state machine.**

## The trade-offs

**Visibility.** With an orchestrator, "where is order #42 stuck?" is a lookup of that instance's state. Under choreography **there is no component that holds the flow**, so the same question is answered only by reconstructing it from distributed traces and from the subscription lists of every participant. The Azure Architecture Center's write-up of the choreography pattern records this consequence directly: the overall business transaction becomes difficult to monitor because no single component is responsible for it.

**Failure handling.** An orchestrator centralises timeouts, retries and — importantly — **compensation ordering**, because it holds the record of which steps have already committed. Under choreography each service must react to every failure event that concerns it, and the reverse-order unwinding is implicit in a sequence no artefact records. The cost shows up on change: adding a "cancel order" path to the choreographed flow touches every participant that has to react to it; in the orchestrated flow it is a new branch in one state machine.

**Coupling.** Choreography is stronger here. Payment need not know that Shipping exists, and new consumers — fraud scoring, analytics, notification — attach to `OrderPlaced` without any change to the publisher. **An orchestrator, by contrast, holds a reference to every participant's interface**, so it changes whenever any participant's contract changes, and when it also absorbs the participants' decision logic the services degrade into create/read/update/delete endpoints.

**Availability.** The orchestrator is an additional component that can be unavailable partway through a flow. Durable-execution engines address this by persisting workflow state as an event history and replaying it after a crash; see [Temporal-style event-sourced workflow state](/articles/microservices/2026-07-31-temporal-durable-execution/).

## Mapping to sagas

Both styles are coordination mechanisms for the same underlying pattern: a [saga](/articles/microservices/2026-07-24-sagas-over-two-phase-commit/), a sequence of local transactions with compensating transactions in place of a distributed lock. Richardson names the two variants explicitly — **choreography-based sagas**, in which each local transaction publishes an event that triggers the next, and **orchestration-based sagas**, in which a saga orchestrator issues commands to participants. His guidance is that choreography suits simple sagas with few steps, and that sagas with branching and compensation are better orchestrated. Either style still requires that a local transaction and the publication of its event commit atomically, which is what the [transactional outbox](/articles/microservices/2026-07-26-transactional-outbox-pattern/) provides.

## The hybrid position

Yan Cui's formulation is to **orchestrate within a bounded context and choreograph between bounded contexts**. Within a context such as order fulfilment the steps are one team's business logic, and an explicit orchestrator makes that logic readable, testable and queryable. Across contexts — fulfilment, notification, analytics, fraud — domain events let other teams subscribe without a shared component, whereas an orchestrator spanning team boundaries couples those teams' release cycles to the orchestrator's.

### Implementation sketch (Scala)

A saga orchestrator is a fold over a step list that records committed steps so that compensation can run in reverse. The load-bearing part is the accumulator, not the transport.

```scala
final case class Step(
    name: String,
    invoke: () => Either[String, Unit],
    compensate: () => Unit
)

enum SagaResult:
  case Committed
  case Compensated(failedAt: String, reason: String)

def runSaga(steps: List[Step]): SagaResult =
  // `done` is the reverse-ordered record of steps that committed;
  // without it the unwinding order is unknown when a later step fails.
  // It lives in memory here, so a crash still loses it: a durable
  // orchestrator persists the equivalent record.
  def loop(remaining: List[Step], done: List[Step]): SagaResult =
    remaining match
      case Nil => SagaResult.Committed
      case step :: rest =>
        step.invoke() match
          case Right(_)     => loop(rest, step :: done)
          case Left(reason) =>
            done.foreach(_.compensate())   // already reverse order
            SagaResult.Compensated(step.name, reason)

  loop(steps, Nil)
```

The choreographed equivalent has no `done` list: each participant infers from an incoming failure event whether its own step needs undoing, so the same ordering knowledge is spread across the participants rather than held in one accumulator.

## Anti-pattern: the implicit workflow

The failure mode worth naming is the **implicit workflow**: a process of many steps expressed as many consumers, each reacting to the event emitted by the previous one. **No artefact states the sequence**, so it can only be recovered by reading every consumer. The observable symptoms are a flow no one can draw, onboarding that requires reading all participants, a reordering of two steps that becomes a multi-team change, and a stalled flow that produces silence rather than an error, because the component that would have detected a missing reply does not exist. A related signal is a growing "process state" payload carried inside events: that payload is an orchestrator's state, held in messages instead of in a component.

## Pitfalls

- **Events that name their recipient.** A message called `OrderPlaced` whose payload includes the handler to run couples publisher to subscriber exactly as a command does, while losing the orchestrator's central state.
- **Compensation without a commit record.** In a choreographed saga a participant that crashes between committing its local transaction and publishing its event leaves no record that the step happened, so no compensation is triggered for it; the transactional outbox exists to close this window.
- **Compensations assumed to be ordered.** Failure events reach participants concurrently, so a refund may be issued before the inventory release it logically follows; correctness must not depend on an ordering the design does not enforce.
- **The orchestrator absorbing participant logic.** When branch conditions that belong to a participant are encoded in the orchestrator, every rule change becomes a change to a component shared by all participants.
- **A non-durable orchestrator.** An orchestrator holding workflow state only in memory loses in-flight flows on restart, and the participants that already committed their local transactions are never compensated.
- **Retries without idempotency keys.** An orchestrator that re-sends a command after a timeout charges twice unless the participant deduplicates on a key supplied by the caller; the timeout says nothing about whether the first attempt committed.
