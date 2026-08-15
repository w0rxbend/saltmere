---
title: "Fowler's four: event notification, event-carried state transfer, event sourcing, CQRS"
date: 2026-08-13
track: microservices
summary: "\"Event-driven\" names four distinct patterns, and conflating them misdirects design reviews. Martin Fowler's taxonomy — event notification, event-carried state transfer, event sourcing, and command query responsibility segregation — with the thin-versus-fat payload choice and the coupling and consistency price of each."
reading_time: 6
tags: [event-driven, event-notification, event-carried-state-transfer, event-sourcing, cqrs]
sources:
  - title: "Martin Fowler — What do you mean by \"Event-Driven\"?"
    url: "https://martinfowler.com/articles/201701-event-driven.html"
  - title: "Martin Fowler — Event Collaboration"
    url: "https://martinfowler.com/eaaDev/EventCollaboration.html"
  - title: "Martin Fowler — Event Sourcing"
    url: "https://martinfowler.com/eaaDev/EventSourcing.html"
  - title: "Martin Fowler — CQRS"
    url: "https://martinfowler.com/bliki/CQRS.html"
---

**Gist.** The phrase "event-driven" covers at least four separate design decisions, and a team that agrees on the phrase can still disagree on every decision under it. Martin Fowler's 2017 essay on the meaning of the term separates them into **event notification, event-carried state transfer, event sourcing, and command query responsibility segregation (CQRS)**, each an independent choice about how a service tells others, how others obtain data, how state is stored, and how reads are modelled against writes. The cost of the separation is that it must be stated explicitly: none of the four implies any other, so an architecture described only as "event-driven" leaves the reader unable to predict its coupling, its consistency, or its failure modes.

## 1. Event notification

The thin variant. A service emits a minimal fact — `customer/1234 changed address` — and interested parties call back to fetch what they need:

```json
{ "type": "CustomerAddressChanged",
  "customerId": "1234",
  "occurredAt": "2026-08-13T09:14:00Z",
  "href": "/customers/1234" }
```

Decoupling on the *sending* side is strong: the emitter neither knows nor records who reacts. Fowler flags the price as **the ease of losing sight of the larger-scale flow**: the flow exists only as the union of subscriptions, and no single artefact states it (the "pinball" problem covered in [choreography vs orchestration](/articles/microservices/2026-08-13-choreography-vs-orchestration/)).

Two further consequences follow from the callback. First, **availability is coupled in the reverse direction of the message flow**: the emitter must be reachable and must absorb the read load its own notifications generate, so a burst of changes produces a proportional burst of inbound reads. Second, **the read is not a read of the event's state**. The consumer receives a notification for version *n* and calls back at a moment when the resource may already be at version *n+k*. Handlers that assume the fetched body corresponds to the event that triggered them are wrong whenever two changes occur within one delivery interval; the observable symptom is a consumer processing the same later state twice and never observing the intermediate state at all.

## 2. Event-carried state transfer

The fat variant. The event carries the data — the new address, or the whole customer record — so consumers keep a local copy and never call back:

```json
{ "type": "CustomerAddressChanged",
  "customerId": "1234",
  "address": { "street": "...", "city": "...", "zip": "..." },
  "occurredAt": "2026-08-13T09:14:00Z" }
```

Consumers now survive the source being unavailable and add no read load to it. The price is **replicated state and eventual consistency**: every consumer holds a copy that is wrong for some interval, and the duration of that interval is bounded by delivery lag under normal operation and unbounded when a consumer is stalled or a message is lost. The event schema also becomes a public contract, since every consumer parses the payload, which brings the compatibility rules described in [schema evolution discipline](/articles/microservices/2026-07-30-schema-evolution-registry-compatibility/).

The thin-versus-fat choice is therefore a choice of **which kind of coupling to accept, not whether to accept coupling**: thin events couple availability, because the consumer's progress depends on the emitter answering a callback; fat events couple schemas, because the consumer's progress depends on the payload remaining parseable.

## 3. Event sourcing

A *storage* decision rather than an integration one: the append-only log of events **is the system of record**, and current state is derived by folding over the log. State can be rebuilt, every decision is auditable, and past states are reconstructible. What is inherited is event versioning for the lifetime of the log, rebuild and snapshot machinery, and the interaction with the outside world during replay that Fowler singles out — replaying events that once caused external calls will cause them again unless those calls are isolated behind a gateway that can be stubbed during replay. The mechanics are covered in [event sourcing: store the decisions, derive the state](/articles/microservices/2026-07-31-event-sourcing-log-as-source-of-truth/).

The taxonomy point is the independence: a service may be event-sourced internally and integrate over plain HTTP, and a service may publish events while storing state in a mutable table. **Publishing `OrderPlaced` to Kafka is not event sourcing**, because nothing about that publication makes the log the source of truth.

## 4. CQRS

Command query responsibility segregation: separate models for writing and reading, where commands mutate a write model and queries hit read models kept in sync — often, though not necessarily, by events. It is the only one of the four not inherently about events. Details in [CQRS: one model to change, another to read](/articles/microservices/2026-08-10-cqrs-read-models/). Fowler's caution is that CQRS belongs on specific portions of a system rather than as a default, because applying it where reads and writes fit the same model adds significant and risky complexity.

## The comparison table

| | Event notification | Event-carried state | Event sourcing | CQRS |
|---|---|---|---|---|
| Answers | "How are others told?" | "How do others get data?" | "How is state stored?" | "How are reads modelled against writes?" |
| Payload | Thin: id + type + link | Fat: full state/delta | Domain events, full fidelity | N/A (commands + queries) |
| Coupling | Low, but callback couples availability | Schema contract; no runtime callback | Internal to the service | Internal to the service |
| Consistency cost | Read-back may see newer state | Replicated copies, eventually consistent | Derivations lag the log | Read models lag writes |
| Main risk | Invisible workflow | Stale copies, schema sprawl | Versioning + replay complexity | Complexity without need |
| Independent? | Yes | Yes | Yes | Yes |

The "Independent?" row carries the argument: **any combination of the four is legal**. A service may use notifications only; carry state without event sourcing; or event-source with CQRS and publish nothing externally.

### Implementation sketch (Scala)

The distinction between the two integration patterns is visible in the consumer's handler signature. A notification handler must perform a fetch and therefore depends on the emitter at handling time; a state-transfer handler is total over its input.

```scala
final case class Address(street: String, city: String, zip: String)

enum CustomerEvent:
  // thin: identity plus a pointer, no state
  case AddressChangedNotification(customerId: String, href: String)
  // fat: the state travels with the event
  case AddressChangedState(customerId: String, address: Address, version: Long)

trait CustomerApi:
  def fetch(href: String): Either[Throwable, (Address, Long)]

final class Projection(api: CustomerApi):
  private var view: Map[String, (Address, Long)] = Map.empty

  def apply(e: CustomerEvent): Either[Throwable, Unit] = e match
    case CustomerEvent.AddressChangedNotification(id, href) =>
      // the fetched pair may already be newer than the event that triggered it
      api.fetch(href).map { fetched => view = merge(id, fetched) }

    case CustomerEvent.AddressChangedState(id, addr, v) =>
      view = merge(id, (addr, v))
      Right(())

  // last-writer-wins on the emitter's version makes replay and reordering harmless
  private def merge(id: String, incoming: (Address, Long)) =
    view.get(id) match
      case Some((_, held)) if held >= incoming._2 => view
      case _                                      => view.updated(id, incoming)
```

The `merge` guard is the load-bearing line: **without a monotonic version supplied by the emitter, a redelivered or reordered event overwrites newer state with older state**, and neither pattern detects it.

## Pitfalls

- A thin event whose consumers all issue an immediate GET is a state-transfer event in denial: the emitter carries the read load of a fat pattern while offering none of its availability benefit, and one schema change to the fetched resource breaks the same consumers a fat payload would have.
- Handlers that treat the callback response as the event's state produce lost intermediate states, because two changes inside one delivery interval yield two fetches of the same later version.
- Projections without a per-entity version applied by last-writer-wins are corrupted by at-least-once redelivery: the duplicate reapplies an older payload over a newer one.
- Publishing internal event-sourced events as the integration contract welds every consumer to the aggregate design, so any refactor of the aggregate becomes a breaking change for consumers; a translation layer emitting purpose-built external events, delivered atomically via the [transactional outbox](/articles/microservices/2026-07-26-transactional-outbox-pattern/), keeps the two schemas separable.
- Adopting event sourcing (pattern 3) to satisfy a requirement that is only notification (pattern 1) commits the service to event versioning and replay machinery for the lifetime of the log, without producing the downstream cache update any faster.
- Replaying an event-sourced log through handlers that call external systems repeats those calls, because the log records the decision and not the fact that its side effect already occurred.
