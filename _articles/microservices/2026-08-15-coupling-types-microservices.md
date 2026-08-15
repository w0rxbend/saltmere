---
title: "The Coupling Taxonomy: Domain, Pass-Through, Common, and Content Coupling"
date: 2026-08-15
track: microservices
summary: "Chapter 2 of Newman's Building Microservices gives coupling a vocabulary: domain coupling (expected), pass-through coupling (a service acts as courier for another service's contract), common coupling (several writers, one row, no owner of the invariants), and content coupling (direct writes into another service's database). Each rung is presented with a service example, its failure mode, its fix, and the two underlying ideas: information hiding and 'code that changes together stays together.'"
reading_time: 6
tags: [coupling, cohesion, information-hiding, service-boundaries, newman, ddd]
sources:
  - title: "Sam Newman — Building Microservices, 2nd Edition (O'Reilly)"
    url: "https://www.oreilly.com/library/view/building-microservices-2nd/9781492034018/"
  - title: "D. L. Parnas — On the Criteria To Be Used in Decomposing Systems into Modules (CACM, 1972)"
    url: "https://dl.acm.org/doi/10.1145/361598.361623"
  - title: "Martin Fowler — Reducing Coupling (IEEE Software)"
    url: "https://martinfowler.com/ieeeSoftware/coupling.pdf"
  - title: "Building Microservices, 2nd Edition — samnewman.io"
    url: "https://samnewman.io/books/building_microservices_2nd_edition/"
---

**Gist.** "Loosely coupled" is asserted about microservice architectures far more often than it is defined, which leaves teams unable to say what is wrong with a boundary they dislike. Chapter 2 of **Building Microservices** (2nd ed.) supplies a definition by adapting the coupling levels of Yourdon and Constantine's *Structured Design* to services. Four of its named rungs — **domain, pass-through, common, content** — are treated here, each with a distinct failure mode and a distinct fix; the chapter also names temporal coupling, which is a synchronous-call concern rather than a data-ownership one and is left aside. The cost of the taxonomy is that its remedies all push work outward: contracts must be owned, state machines must be centralised in one service, and back doors into another service's storage must be closed, which converts cheap direct access into API calls, events, or replicated data.

## The two underlying ideas

**Information hiding** is Parnas's 1972 criterion: a module boundary should hide the decisions most likely to change and expose the smallest stable interface. For a service, the decision most in need of hiding is the **database schema**, because it is the part a service changes most freely while believing nobody else depends on it. Newman applies the criterion directly to services: expose as little as the collaboration requires and hide the rest.

**Cohesion** is the complementary measurement — *"the code that changes together, stays together."* A boundary is well placed when a typical product change lands inside a single service, and badly placed when adding one field to an invoice touches four repositories and requires a choreographed deployment order. Coupling and cohesion are one property observed from two sides: **weak cohesion inside a boundary is observable as tight coupling across it**, because the change that could not be contained has to cross the line.

The four rungs follow, loosest first.

## Domain coupling: expected, still worth minimising

**Domain coupling** is one service invoking another because the business domain requires it — an Order Processor calling Warehouse to reserve stock and Payment to take money. This is not a defect; it is the system performing its function. It becomes a signal only in degree: a service that synchronously calls Loyalty, Fraud, Email, and Recommendations in addition to Warehouse and Payment indicates that logic belonging to those domains has centralised in the caller. Two mitigations apply. **Emitting an event** such as `OrderPlaced` and letting interested services subscribe inverts the direction of knowledge — the emitter no longer names its consumers. **Sending only the fields the callee needs**, rather than the caller's whole internal order representation, narrows the surface whose change can break the callee.

## Pass-through coupling: the courier

**Pass-through coupling** is a service transporting data through itself, untouched, because a service further downstream needs it. In the worked example, Order Processor accepts a `ShippingManifest` from the user interface and forwards it unread to Shipping. The failure mode is precise: **a change to Shipping's manifest format forces a release of Order Processor, a service that never reads the field**. The middleman's deployment cadence is now bound to a contract it does not participate in.

```text
BEFORE  UI --{order, ShippingManifest}--> OrderProcessor --{ShippingManifest}--> Shipping
        (Shipping's contract change forces an OrderProcessor release)

FIX A   OrderProcessor sends {address, items}; Shipping builds its own manifest
FIX B   UI talks to Shipping directly for the manifest concern
FIX C   Treat the manifest as an opaque blob OrderProcessor never parses
```

Fix A is the one Newman recommends: **the downstream service owns its contract** and constructs its internal representation from meaningful domain fields such as address and line items, which change on the domain's schedule rather than on Shipping's. Fix C removes the recompilation dependency without moving the ownership — Order Processor stops breaking when the format changes, but still carries data it cannot interpret. Fix B eliminates the hop by making the coupling between the user interface and Shipping direct and visible, converting pass-through coupling into domain coupling.

## Common coupling: several writers, one row

**Common coupling** is multiple services reading and, critically, **writing** the same data: a shared `order_status` column, a shared configuration table, a shared file. Shared reads are survivable; shared writes are the failure. When Warehouse, Payment, and Fulfilment each set order state independently, **no single component enforces which transitions are legal**, so Warehouse can write `DESPATCHED` to an order that Payment has already moved to `PAYMENT_FAILED`. The state machine exists in the team's collective head and in no executable location. A second, operational cost accompanies the correctness one: every writer contends on the same row, serialising updates that have no logical reason to be ordered.

The fix is the **single writer principle**. Exactly one service — Order — owns the status field and its transition rules. Other services *request* a transition through Order's application programming interface (API) and can be refused, with the rejection naming the illegal transition (`PAYMENT_FAILED → DESPATCHED`). Two properties follow: the invariants acquire a single home that can be tested, and the underlying schema returns to being hidden, restoring the information-hiding property that the shared column destroyed.

### Implementation sketch (Scala)

The load-bearing part of the single writer principle is that the legal transition set is data owned by one service, and every external request is checked against it before any write occurs.

```scala
enum OrderStatus:
  case Placed, PaymentFailed, Paid, Despatched, Cancelled

final case class IllegalTransition(from: OrderStatus, to: OrderStatus)

object OrderStateMachine:
  import OrderStatus.*

  // The only place in the system that knows which transitions exist.
  private val allowed: Map[OrderStatus, Set[OrderStatus]] = Map(
    Placed        -> Set(Paid, PaymentFailed, Cancelled),
    PaymentFailed -> Set(Paid, Cancelled),   // a retried payment may still succeed
    Paid          -> Set(Despatched, Cancelled),
    Despatched    -> Set.empty,
    Cancelled     -> Set.empty
  )

  def transition(from: OrderStatus, to: OrderStatus): Either[IllegalTransition, OrderStatus] =
    if allowed.getOrElse(from, Set.empty).contains(to) then Right(to)
    else Left(IllegalTransition(from, to))

// Callers hold no write access to the column; they submit a requested target state.
def requestTransition(orderId: String, to: OrderStatus): Either[IllegalTransition, OrderStatus] =
  val current = loadStatus(orderId)              // owning service's own storage
  OrderStateMachine.transition(current, to).map: next =>
    storeStatus(orderId, expected = current, next = next)  // compare-and-set on the row
    next
```

The compare-and-set on the stored value matters because `loadStatus` and `storeStatus` are not atomic together: **without the expected-value check, two concurrent requests can each read `Paid`, each find their target legal, and both write**, reproducing the lost-update behaviour the single writer was introduced to remove.

## Content coupling: writing another service's database

**Content coupling** is a service reaching into another service's internals and modifying them directly, the canonical instance being writes to another service's database tables that bypass its API. It resembles common coupling but differs in kind rather than degree. Under common coupling the shared item is *known* to be shared, so its owner expects other writers. Under content coupling **the owning team believes the schema is private and refactors it accordingly**, at which point the intruding job either fails or writes rows that violate constraints the API would have rejected — every validation the owner performs at its boundary is skipped. The rule Newman states is absolute: **do not reach into another service's database**. Data required from elsewhere is obtained through an API, an event subscription, or a published data product — some contract the owner exposed deliberately and therefore maintains.

## The ladder

| Coupling | What it looks like | Severity | Fix |
|---|---|---|---|
| **Domain** | Order calls Warehouse's API | Loose — expected | Events over calls; send minimal payloads |
| **Pass-through** | Order forwards Shipping's manifest unread | Moderate | Downstream owns its contract; or bypass/opaque blob |
| **Common** | Several services write one shared table | Tight | Single writer owns state + invariants |
| **Content** | Service writes another's DB directly | Tightest — never acceptable | An actual API, events, or data replication |

Naming the rung is what makes the diagnosis actionable. "The services share a database" is ambiguous between common and content coupling, and the two remedies are different: common coupling requires an owner to be appointed for the shared state, whereas content coupling requires the unsanctioned access path to be removed. The test for whether a remedy worked is the cohesion slogan applied in reverse — after the change, does the code that changes together live together?

A concrete audit follows from the definition of content coupling: cross-service database grants in the infrastructure configuration (`GRANT ... ON orders.* TO finance_svc`) are content coupling that is live in production, and each one is a candidate for replacement by an event subscription.

## Pitfalls

- **A shared read-only view is treated as safe and then acquires writers.** The read grant does not enforce read-only over time; once a second service writes through it, the state machine has no owner and illegal transitions become possible without any schema change to signal the shift.
- **Pass-through coupling is "fixed" by making the payload opaque, and the domain problem is left in place.** The middleman stops recompiling, but the field still traverses a service that cannot validate it, so a malformed manifest is detected only at the final hop.
- **The single writer is introduced without a compare-and-set.** Concurrent transition requests both read the same current state, both pass the legality check, and both write; the lost update looks identical to the common-coupling symptom the change was meant to remove.
- **Events are added on top of the synchronous calls rather than replacing them.** The caller still names every consumer in its call graph, so the coupling is unchanged while the failure surface has grown by one asynchronous path.
- **Full internal entities are published as event payloads.** Every field of the emitter's internal model becomes part of its public contract by consumption, and a field renamed for internal reasons breaks subscribers the emitter does not know about.
- **Content coupling is discovered only when the owning team refactors.** The intruding job runs correctly for as long as the private schema happens to be stable, so the absence of failures is not evidence that the access path is sanctioned.
