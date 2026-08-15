---
title: "Fowler's four: event notification, event-carried state transfer, event sourcing, CQRS"
date: 2026-08-13
track: microservices
summary: "\"We're event-driven\" can mean four different patterns, and conflating them is how design reviews go sideways. Martin Fowler's taxonomy: event notification, event-carried state transfer, event sourcing, and CQRS — what each one actually is, thin vs fat payloads, and the coupling and consistency price of each."
reading_time: 5
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

When a team says "we're event-driven," Martin Fowler's observation is that they may mean any of **four different patterns** — and that people talking past each other about which one is in play is a genuine source of bad architecture. His 2017 essay "What do you mean by 'Event-Driven'?" names them: event notification, event-carried state transfer, event sourcing, and CQRS. They compose, but they are separate decisions with separate costs. Interviews love this distinction precisely because most candidates blur it.

## 1. Event notification

The thin one. A service emits a minimal fact — `customer/1234 changed address` — and interested parties call back to fetch whatever they need:

```json
{ "type": "CustomerAddressChanged",
  "customerId": "1234",
  "occurredAt": "2026-08-13T09:14:00Z",
  "href": "/customers/1234" }
```

You get superb decoupling on the *sending* side: the emitter neither knows nor cares who reacts. The price Fowler flags is that **no statement of overall behavior exists anywhere** — the logic is implicit in who subscribes to what (the "pinball" problem covered in [choreography vs orchestration](/articles/microservices/2026-08-13-choreography-vs-orchestration/)). Consumers also call back to the source, so it must stay up and handle the read load, and the state they read may already be newer than the event that prompted the read.

## 2. Event-carried state transfer

The fat one. The event carries the data itself — the full new address, or the whole customer record — so consumers keep their own copy and never call back:

```json
{ "type": "CustomerAddressChanged",
  "customerId": "1234",
  "address": { "street": "...", "city": "...", "zip": "..." },
  "occurredAt": "2026-08-13T09:14:00Z" }
```

Now consumers survive the source being down and add zero read load to it. The price is **replicated state and eventual consistency**: every consumer holds a copy that is briefly (or, after a bug, not so briefly) wrong, and the event schema becomes a public contract — changing it means [schema evolution discipline](/articles/microservices/2026-07-30-schema-evolution-registry-compatibility/). The thin-vs-fat payload choice is the practical knob here: thin events couple availability (callback needed), fat events couple schemas (everyone parses your record).

## 3. Event sourcing

A *storage* decision, not an integration one: the append-only log of events **is the system of record**, and current state is derived by folding over it. You can rebuild state, audit every decision, and time-travel. You also inherit event versioning forever, rebuild/snapshot machinery, and the interesting wrinkle Fowler notes about interactions with the outside world during replay. The corpus covers the mechanics in [event sourcing: store the decisions, derive the state](/articles/microservices/2026-07-31-event-sourcing-log-as-source-of-truth/) — the taxonomy point is: you can event-source a service that integrates via plain REST, and you can publish events without event-sourcing anything. Publishing `OrderPlaced` to Kafka does not mean you "do event sourcing."

## 4. CQRS

Separate models for writing and reading — commands mutate a write model, queries hit denormalized read models kept in sync (often by events, not necessarily). It's the only one of the four that isn't inherently about events at all; it just pairs well with them. Details in [CQRS: one model to change, another to read](/articles/microservices/2026-08-10-cqrs-read-models/). Fowler's caution: used judiciously it's valuable, used as a default it adds "a lot of complexity for little gain."

## The comparison table

| | Event notification | Event-carried state | Event sourcing | CQRS |
|---|---|---|---|---|
| Answers | "How do I tell others?" | "How do others get data?" | "How do I store state?" | "How do I model reads vs writes?" |
| Payload | Thin: id + type + link | Fat: full state/delta | Domain events, full fidelity | N/A (commands + queries) |
| Coupling | Low, but callback couples availability | Schema contract; no runtime callback | Internal to the service | Internal to the service |
| Consistency cost | Read-back may see newer state | Replicated copies, eventually consistent | Derivations lag the log | Read models lag writes |
| Main risk | Invisible workflow | Stale copies, schema sprawl | Versioning + replay complexity | Complexity without need |
| Independent? | Yes | Yes | Yes | Yes |

The "Independent?" row is the interview answer: **any combination is legal.** A service can use notifications only; carry state with no event sourcing; event-source with CQRS but publish nothing externally. Each is a separate trade.

## Why the conflation hurts

Concretely: a team hears "event-driven," adopts event sourcing for a CRUD service (pattern 3) when all they needed was to notify a downstream cache (pattern 1) — and now they maintain event versioning forever. Or they publish their event-sourced *internal* events as the integration contract, welding every consumer to their aggregate design; the fix is a translation layer emitting purpose-built external events, with delivery made atomic via the [transactional outbox](/articles/microservices/2026-07-26-transactional-outbox-pattern/). Naming which of the four patterns a proposal actually uses — and which it deliberately doesn't — is the cheapest design review trick there is.

**Try next:** take one event your system publishes today and classify it: notification or state transfer? Then check whether its consumers agree — a thin event that three consumers immediately follow with a GET to your API is a state-transfer event in denial, and one schema change away from proving it.
