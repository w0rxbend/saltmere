---
title: "Event sourcing: store the decisions, derive the state"
date: 2026-07-31
track: microservices
summary: "A service that stores current state discards the history that produced it. Event sourcing inverts the arrangement: an append-only log of state-changing events is the source of truth, and current state is a fold over that log. The audit trail and retroactive projections come at the price of permanent schema evolution, snapshotting and eventually consistent reads."
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

**Gist.** A row that holds `balance = 80` records the outcome of a history it has already destroyed: which deposits and withdrawals occurred, in what order, at what times. Event sourcing stores the **full sequence of state-changing events** as an append-only log and derives current state by replaying it, so the history is retained by construction. The cost is that events are immutable and kept indefinitely, which turns schema evolution, load latency and erasure of personal data into standing engineering obligations rather than one-off migrations.

## The mechanics

The unit of consistency is an **aggregate** — an account, an order, a cart. Each aggregate owns a stream of immutable, past-tense events identified by a monotonically increasing sequence number:

```
stream: account-42
  seq 1  AccountOpened      { owner: "sam", opened: "2026-07-01" }
  seq 2  MoneyDeposited     { amount: 100, at: "2026-07-02T09:00Z" }
  seq 3  MoneyWithdrawn     { amount: 30,  at: "2026-07-04T14:12Z" }
  seq 4  MoneyDeposited     { amount: 10,  at: "2026-07-05T10:01Z" }
```

Current state is a left fold: an `apply` function threads each event through a running value, yielding `balance = 80` for the stream above. The fold must be a **pure, total function of the event sequence** — an aggregate reconstructed twice from the same prefix must produce the same state, otherwise replay and live execution disagree.

A command handler follows a fixed cycle: load the stream, fold it to current state, validate the command against that state, and, if the command is admissible, **append one or more new events**. No row is updated; the only write primitive is append. The guarantee the event store must supply is an **optimistic-concurrency append** — "append at expected sequence *N*" — which fails if another writer has already committed *N*. In a relational store the guarantee reduces to a **unique constraint on `(stream_id, seq)`**: the losing transaction's insert violates the constraint and rolls back.

That conditional append is the entire concurrency-control mechanism. The invariant it protects is that **every event in a stream was validated against the exact state produced by folding all its predecessors**. Without it, two handlers could both fold a stream ending at seq 41, both find the balance sufficient, and both append a withdrawal at seq 42 — the second one spending money the first had already committed. With it, one append fails and the handler re-reads, re-folds and re-decides against the newer state, which may now reject the command.

## Properties the log provides

- **A complete audit trail.** Every change is a timestamped, first-class fact, so "how did this value arise" is answerable without additional bookkeeping. Domains with statutory audit obligations — finance, healthcare, logistics — are the ones for which this property is cited as the primary motivation.
- **Historical queries.** Folding a prefix of the stream reconstructs the state as of any earlier point, which supports both debugging and retrospective reporting.
- **Retroactive read models.** Because no event was discarded, a projection invented later can be populated by replaying the existing history through a new fold, rather than by a backfill migration that has no data to draw on.
- **Integration by subscription.** The stored events are also the published events, so a downstream service consumes the stream instead of polling a table for changes.

## Pairing with CQRS

Folding a long stream on every read is impractical, so event-sourced systems are commonly combined with **Command Query Responsibility Segregation (CQRS)**: the write side appends events, and a projector consumes the stream to maintain denormalized **read models** — a plain `balances` table, a search index — shaped for queries. The two sides are commonly separate stores kept in step by the log, and the read side is therefore **eventually consistent**: a projection trails the log by whatever the projector's processing lag happens to be, and an interface that reads its own writes immediately after a command may observe the pre-command value.

## Costs the pattern imposes

- **Schema evolution has no end state.** Events are immutable and retained indefinitely, so an old `MoneyDeposited` payload must remain deserializable for as long as the stream exists. This requires explicit event versioning and **upcasters** — functions that translate an old payload into the current shape at read time — because the past cannot be altered by a schema migration.
- **Load latency grows with stream length.** Replaying a long stream to load one aggregate becomes the dominant cost of every command. The standard remedy is a **snapshot**: persist the folded state at sequence *N*, then on load start from that snapshot and replay only events after *N*. A snapshot is a cache derived from the log, never a substitute for it — deleting all snapshots must leave the system correct.
- **Erasure conflicts with append-only storage.** A right-to-erasure request cannot be served by deleting a row. The available approaches are crypto-shredding — encrypt a subject's payloads under a per-subject key and destroy the key — or stream design that keeps personal data out of retained events.
- **The pattern is disproportionate for CRUD domains.** Where the domain is create, read, update and delete over records whose history carries no business meaning, event sourcing adds machinery without a corresponding return. Its value comes from the history and the recorded intent behind each change.

### Implementation sketch (Scala)

```scala
enum Event:
  case Opened(owner: String)
  case Deposited(amount: Long)
  case Withdrawn(amount: Long)

final case class Account(open: Boolean = false, balance: Long = 0):
  def apply(e: Event): Account = e match
    case Event.Opened(_)       => copy(open = true)
    case Event.Deposited(a)    => copy(balance = balance + a)
    case Event.Withdrawn(a)    => copy(balance = balance - a)

/** Snapshot carries the sequence it was taken at; replay resumes from there. */
final case class Snapshot(seq: Long, state: Account)

trait EventStore:
  def load(id: String, fromSeq: Long): Vector[(Long, Event)]
  def snapshot(id: String): Option[Snapshot]
  /** Fails if another writer already committed `expectedSeq`. */
  def append(id: String, expectedSeq: Long, e: Event): Either[Conflict, Unit]

final case class Conflict(actualSeq: Long)

def rebuild(store: EventStore, id: String): (Long, Account) =
  val base = store.snapshot(id).getOrElse(Snapshot(0L, Account()))
  store.load(id, base.seq).foldLeft(base.seq -> base.state):
    case ((_, st), (seq, e)) => (seq, st.apply(e))

def withdraw(store: EventStore, id: String, amount: Long): Either[String, Unit] =
  val (seq, st) = rebuild(store, id)
  if !st.open then Left("account closed")
  else if amount > st.balance then Left("insufficient funds")
  // a Conflict means the fold is stale: the caller must re-read and re-decide,
  // not resubmit the same event at a higher sequence
  else store.append(id, expectedSeq = seq + 1, Event.Withdrawn(amount))
    .left.map(c => s"conflict at seq ${c.actualSeq}")
```

The validation reads only derived state, and the sole write is an append guarded by the expected sequence.

## Pitfalls

- **Retrying a rejected append without re-folding reintroduces the lost update.** The conflict signals that the state the command was validated against no longer holds; replaying the same event at the new sequence commits a decision that was never checked.
- **A non-deterministic `apply` — reading the clock, a random value or an external service — makes replay diverge from the original run.** Any such value must be captured *in* the event at append time.
- **Treating a snapshot as authoritative hides fold bugs.** If snapshots are never invalidated and rebuilt from seq 0, a defect in `apply` is frozen into the snapshot and never observed again.
- **Publishing events to consumers before the append commits produces phantom facts.** A consumer acts on an event whose transaction later rolls back; the log and the downstream view disagree permanently.
- **An expressive event vocabulary that leaks internal fields becomes an unversionable public contract.** Every subscriber and every upcaster must accommodate each retained field forever, so fields added for internal convenience cannot later be withdrawn.
- **Assuming a command is visible to the query side immediately yields intermittent, load-dependent bugs.** The interface observes pre-command values whenever projector lag exceeds the interval between the command and the following read.
