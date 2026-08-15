---
title: "The Coupling Taxonomy: Domain, Pass-Through, Common, and Content Coupling"
date: 2026-08-15
track: microservices
summary: "Chapter 2 of Newman's Building Microservices gives coupling a vocabulary most engineers lack: domain coupling (fine), pass-through coupling (you're a courier for someone else's contract), common coupling (three writers, one row, no invariants), and content coupling (reaching into another service's database — just don't). Here is each one with a concrete service example, the fix, and the two ideas underneath: information hiding and 'code that changes together stays together.'"
reading_time: 5
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

Everyone agrees microservices should be "loosely coupled" and nobody defines it. Chapter 2 of **Building Microservices** (2nd ed.) fixes that by resurrecting a taxonomy from 1970s structured programming — Constantine and Yourdon's coupling levels — and translating it to services. The payoff: instead of "these services feel too coupled," you can say *which kind* of coupling you have, and each kind has a known fix.

## The two ideas underneath

**Information hiding** is Parnas's 1972 criterion: a module boundary should hide the decisions most likely to change, exposing the smallest stable interface. For services, the hidden thing is above all the **database schema** — the single most change-prone decision a service owns. Newman's formulation: share as little as you can get away with.

**Cohesion** is the flip side: *"the code that changes together, stays together."* A service boundary is good when a typical product change lands inside one service; it is bad when adding a field to an invoice touches four repos and needs a choreographed deploy. Coupling and cohesion are the same force measured in two places — weak cohesion inside a boundary shows up as tight coupling across it.

Now the spectrum, loosest to tightest.

## Domain coupling: fine, minimize anyway

**Domain coupling** is one service using another's functionality because the business domain demands it: Order Processor calls Warehouse to reserve stock and Payment to take money. This is unavoidable — it *is* the system working. It becomes a smell only in degree: a service that talks to everything (an Order Processor that also calls Loyalty, Fraud, Email, and Recommendations synchronously) is a sign logic is centralizing that belongs elsewhere. Mitigate with events — emit `OrderPlaced` and let interested parties subscribe — and by sending only what the callee needs, not your whole internal order object.

## Pass-through coupling: you're a courier

**Pass-through coupling** is a service passing data through itself untouched because a downstream service needs it. Example: Order Processor accepts a `ShippingManifest` from the UI and forwards it, unread, to Shipping. Now a change to Shipping's manifest format ripples through Order Processor — a service that doesn't even *use* the field must be redeployed for it.

```text
BEFORE  UI --{order, ShippingManifest}--> OrderProcessor --{ShippingManifest}--> Shipping
        (Shipping's contract change forces an OrderProcessor release)

FIX A   OrderProcessor sends {address, items}; Shipping builds its own manifest
FIX B   UI talks to Shipping directly for the manifest concern
FIX C   Treat the manifest as an opaque blob OrderProcessor never parses
```

The real fix is A: **let the downstream service own its contract** and construct its own internal representations from meaningful domain fields. C at least stops the middleman from breaking when the format changes; B rearranges the domain coupling honestly.

## Common coupling: three writers, one row

**Common coupling** is multiple services reading and *writing* the same data — a shared `order_status` column, a shared config table, a shared file. Reads are survivable; writes are the poison. When Warehouse, Payment, and Fulfillment each flip order state, nobody owns the state machine: Warehouse can set `DESPATCHED` on an order Payment just moved to `PAYMENT_FAILED`, because no single place enforces which transitions are legal. You also get operational contention — every writer serializing on one hot row.

Fix: the **single writer principle**. Exactly one service — Order — owns the status and its state machine. Everyone else *requests* a change through Order's API and can be told no ("invalid transition `PAYMENT_FAILED → DESPATCHED`"). The invariants get a home, and the schema underneath goes back to being hidden.

## Content coupling: reaching into someone's DB

**Content coupling** is a service reaching into another's internals and modifying them directly — the canonical case being writing to another service's database tables, bypassing its API. It looks like common coupling but is worse in kind, not degree: with common coupling the shared thing is at least *known* to be shared; with content coupling, Order's team believes its schema is private and refactors it, and Finance's nightly job silently corrupts or crashes. All the validation Order's API performs is bypassed. Newman's verdict is blunt, and the rule is absolute: **never reach into another service's database.** If you need the data, ask for an API, subscribe to its events, or consume a published data product — anything that goes through a contract the owner deliberately exposed.

## The ladder

| Coupling | What it looks like | Severity | Fix |
|---|---|---|---|
| **Domain** | Order calls Warehouse's API | Loose — expected | Events over calls; send minimal payloads |
| **Pass-through** | Order forwards Shipping's manifest unread | Moderate | Downstream owns its contract; or bypass/opaque blob |
| **Common** | Several services write one shared table | Tight | Single writer owns state + invariants |
| **Content** | Service writes another's DB directly | Tightest — never OK | An actual API, events, or data replication |

The interview move is to name the rung. "We share a database" is ambiguous between common and content coupling, and the fixes differ: common coupling needs an owner appointed; content coupling needs the back door bricked up. And the test for whether any fix worked is cohesion's slogan run in reverse — after the change, does the code that changes together finally live together?

**Try next:** grep your infrastructure for cross-service DB grants (`GRANT ... ON orders.* TO finance_svc`) — each one is content coupling in production today; pick the worst and replace it with an event subscription.
