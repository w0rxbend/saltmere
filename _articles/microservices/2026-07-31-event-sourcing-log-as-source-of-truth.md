---
title: "Event sourcing: store the decisions, derive the state"
date: 2026-07-31
track: microservices
summary: "Most services store the current state and throw away how they got there. Event sourcing inverts that: the append-only log of what happened is the source of truth, and current state is a fold over it. You gain a perfect audit trail and time travel — and inherit real costs around schema evolution and rebuilds."
reading_time: 6
tags: [event-sourcing, cqrs, event-store, aggregates, snapshots, audit]
sources:
  - title: "Chris Richardson — Pattern: Event sourcing (microservices.io)"
    url: "https://microservices.io/patterns/data/event-sourcing.html"
  - title: "Martin Fowler — Event Sourcing"
    url: "https://martinfowler.com/eaaDev/EventSourcing.html"
  - title: "Microsoft Azure Architecture Center — Event Sourcing pattern"
    url: "https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing"
  - title: "Martin Fowler — CQRS"
    url: "https://martinfowler.com/bliki/CQRS.html"
  - title: "Greg Young — CQRS Documents (event sourcing rationale)"
    url: "https://cqrs.files.wordpress.com/2010/11/cqrs_documents.pdf"
---

A normal `accounts` table stores `balance = 80`. If a customer disputes it, you can say the balance is 80 — but not that it started at 100, that a $30 withdrawal and a $10 deposit happened, in that order, at those times. The moment you wrote `80` you destroyed the history that produced it. For a bank, a warehouse, or anything with an auditor, that lost history is often the most valuable data you had.

Event sourcing keeps it. Instead of storing current state and mutating it in place, you store the **full sequence of state-changing events** as an append-only log, and you *derive* current state by replaying them.

## The mechanics

The unit of consistency is an **aggregate** (an account, an order, a cart). For each aggregate you persist a stream of immutable, past-tense events:

```
stream: account-42
  seq 1  AccountOpened      { owner: "sam", opened: "2026-07-01" }
  seq 2  MoneyDeposited     { amount: 100, at: "2026-07-02T09:00Z" }
  seq 3  MoneyWithdrawn     { amount: 30,  at: "2026-07-04T14:12Z" }
  seq 4  MoneyDeposited     { amount: 10,  at: "2026-07-05T10:01Z" }
```

Current state is a left fold — `apply` each event to a running value:

```python
def rebuild(events):
    state = {"balance": 0, "open": False}
    for e in events:
        if e.type == "AccountOpened":   state["open"] = True
        elif e.type == "MoneyDeposited": state["balance"] += e.amount
        elif e.type == "MoneyWithdrawn": state["balance"] -= e.amount
    return state          # -> {"balance": 80, "open": True}
```

A command handler loads the stream, folds it to get current state, validates the command against that state, and — if valid — **appends a new event**. It never updates a row; it only ever appends. The event store's one hard guarantee is an **optimistic-concurrency append**: "append this event at expected sequence N," which fails if someone else already wrote N. That single conditional-append is what keeps two concurrent commands from corrupting an aggregate.

## Why teams reach for it

The append-only log gives you things a mutable table cannot:

- **A real audit trail, for free.** Every change is a first-class, timestamped fact. "How did we get to this balance?" is answerable by construction, which is why finance, healthcare and logistics gravitate to it.
- **Time travel.** Fold up to `seq 3` and you have the exact state as of last Tuesday — priceless for debugging and for regulators.
- **Retroactive read models.** Because you kept every event, you can build a *new* projection of old data. Realize months later you want a "monthly spend" view? Replay history through a new fold and it is fully populated — no backfill migration.
- **A natural fit for messaging.** The events you store are the events you publish, so integrating other services becomes "subscribe to the stream" rather than "poll a table."

## Event sourcing pairs with CQRS

Folding a long stream on every read is impractical, so event-sourced systems almost always adopt **CQRS** (Command Query Responsibility Segregation): the write side appends events; a projector consumes them and maintains denormalized **read models** (a plain `balances` table, an Elasticsearch index) optimized for queries. The two sides are separate stores kept in sync by the event stream. This is powerful but it makes the read side **eventually consistent** — a projection may lag the log by milliseconds to seconds, and your UI has to tolerate "I just did that; why don't I see it yet?"

## The costs are real — go in with eyes open

- **Schema evolution never ends.** Events are immutable and you keep them *forever*, so a five-year-old `MoneyDeposited` v1 must still deserialize. You need explicit event versioning and upcasters from day one; you can't `ALTER TABLE` the past.
- **Rebuilds get slow → snapshots.** Folding 200k events to load one aggregate is unacceptable. The standard fix is a **snapshot**: periodically persist the folded state at seq N, then on load start from the snapshot and replay only events after it.
- **Deletes are hard.** "Append-only" collides with GDPR's right to erasure. You end up with crypto-shredding (drop the key that decrypts a subject's event payloads) or careful stream design — not an afterthought.
- **It's overkill for CRUD.** If your domain is genuinely "edit a profile, show a profile," event sourcing adds cost with little payoff. It earns its keep where the *history and the intent behind changes* have business value.

## Sketch of the write path

```python
def withdraw(store, account_id, amount):
    events = store.load(account_id)          # read the stream
    state  = rebuild(events)                  # fold to current state
    if not state["open"]:      raise Error("account closed")
    if amount > state["balance"]: raise Error("insufficient funds")
    # append at the expected next sequence; store rejects on conflict
    store.append(account_id,
                 expected_seq=len(events),
                 event=("MoneyWithdrawn", {"amount": amount}))
```

Notice the validation happens against *derived* state, and the only write is an append guarded by `expected_seq`. That is the whole pattern in nine lines.

**Try next:** take one aggregate in an existing service — a shopping cart is ideal — and re-model it as events (`ItemAdded`, `ItemRemoved`, `CheckedOut`) backed by a single `events(stream_id, seq, type, payload)` table with a unique constraint on `(stream_id, seq)`. Write the `rebuild` fold, then add a snapshot once a stream passes 100 events and measure how much faster load gets. That constraint plus that fold is event sourcing; everything else is optimization.
