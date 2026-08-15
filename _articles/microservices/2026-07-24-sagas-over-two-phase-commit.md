---
title: "Sagas over two-phase commit: consistency without a distributed lock"
date: 2026-07-24
summary: "When a business operation spans several services, a distributed transaction is the tempting answer and usually the wrong one. Sagas trade atomicity for availability; this article walks the mechanism, the compensation order, and the isolation window it opens."
track: microservices
reading_time: 7
tags: [sagas, transactions, consistency, orchestration, newman]
sources:
  - title: "Sam Newman, Building Microservices (2nd ed.), ch. 6 — Workflow"
    url: "https://www.oreilly.com/library/view/building-microservices-2nd/9781492034018/"
  - title: "Hector Garcia-Molina & Kenneth Salem, Sagas (1987)"
    url: "https://www.cs.cornell.edu/andru/cs711/2002fa/reading/sagas.pdf"
---

**Gist.** A business operation that spans several services — reserve stock, charge a card, schedule shipping — has no single database in which it can commit atomically. A **saga** replaces the global commit with a sequence of local transactions, each atomic inside one service, plus a **compensating** transaction for each step that semantically undoes it. The cost is the loss of isolation: the partially-applied state between steps is visible to every other reader in the system, and the application, not the database, becomes responsible for what that state means.

## What a two-phase commit costs across services

Two-phase commit (2PC) makes a multi-participant operation atomic by splitting it into a *prepare* round, in which every participant votes and durably promises to be able to commit, and a *commit* round, in which the coordinator broadcasts the outcome. Newman's chapter 6 sets out the objections that matter at service granularity:

- **Locks are held for the duration of the slowest participant.** A participant that has voted to prepare cannot release the resources it promised until the coordinator's second round arrives, so the contention window of every participant is set by the worst one.
- **The participants become a single availability unit.** The operation succeeds only if all participants and the coordinator are reachable, which composes their availabilities downward rather than keeping them independent.
- **The window between prepare and commit is a real failure mode.** A coordinator that fails after collecting votes and before broadcasting the outcome leaves participants holding prepared state with no way to decide locally whether to commit or abort.

None of this makes 2PC wrong; it makes it expensive in latency and coupling in exactly the setting — many independently deployed services — where microservice architectures are supposed to buy independence.

## The saga structure and its invariant

Garcia-Molina and Salem define a saga as a sequence of transactions **T₁ … Tₙ** together with compensating transactions **C₁ … Cₙ₋₁**, where each Cᵢ semantically undoes Tᵢ. The system's guarantee is not atomicity. It is that **the sequence eventually observed is either the complete forward run T₁ … Tₙ, or a prefix followed by its compensations in reverse order: T₁ … Tⱼ, Cⱼ, Cⱼ₋₁ … C₁**. Nothing else is a legal outcome, and reaching one of those two terminal states is the whole obligation of a saga implementation.

Two consequences follow directly from that shape.

**Reverse order is load-bearing, not cosmetic.** Compensations run against the state each forward step left behind, and later steps may depend on earlier ones. Running C₁ before C₂ can remove the record C₂ needs to identify what it is undoing.

**The last step needs no compensation.** Cₙ is absent from the definition: once Tₙ commits, the saga is in its successful terminal state and nothing remains to unwind. The corollary for decomposition is that a step whose compensation is weakest — hardest to undo semantically — is never invoked in an unwind if it is placed last.

## Compensation is a new business action

A compensation is not a rollback. A rollback restores prior state by discarding uncommitted writes; a compensating transaction is an ordinary transaction that commits new state whose business meaning is the negation of the original. **A charged card is not un-charged; it is refunded, and both the charge and the refund remain permanently on the statement.** A dispatched email is not unsent; a correction is sent. Where no such action exists, the step cannot be compensated and the saga decomposition is wrong.

### Implementation sketch (Scala)

The orchestrated form keeps the sequence in one place. The load-bearing details are the accumulated log of completed steps and the reverse traversal of it; everything else is plumbing.

```scala
final case class Step[A](
    name: String,
    run: () => A,           // a local transaction in one service
    compensate: A => Unit   // the semantic undo, applied to that step's own result
)

enum Outcome:
  case Completed
  case CompensatedAt(step: String, cause: Throwable)

/** Runs steps in order; on failure compensates the completed prefix in reverse. */
def runSaga(steps: List[Step[?]]): Outcome =
  // One closure per completed step, each capturing that step's own result.
  var done: List[() => Unit] = Nil
  val it = steps.iterator
  while it.hasNext do
    val step = it.next().asInstanceOf[Step[Any]]
    try
      val result = step.run()
      done = (() => step.compensate(result)) :: done // prepend: head is the newest
    catch
      case cause: Throwable =>
        done.foreach(_()) // already in reverse order of execution
        return Outcome.CompensatedAt(step.name, cause)
  Outcome.Completed
```

The sketch omits what production requires and the next section names: the compensation loop must itself be retried until each compensation succeeds, since abandoning it halfway leaves the saga in neither terminal state.

## Idempotency

A step invoked over a network can time out after the remote transaction has committed. The caller cannot distinguish that case from a step that never ran, so its only safe response is to retry — which means **every step and every compensation must be safe to apply more than once**. The usual mechanism is a business idempotency key carried with the request, such as the order identifier, against which the receiving service deduplicates: a second `charge` for a key it has already charged returns the original result rather than charging again. Deduplication must live in the receiving service, because only that service knows whether its own transaction committed.

## The isolation window

Between T₁ and Tₙ the system is in a state no single-database transaction would expose: stock reserved against an order that has not been paid for. Other operations can read it. A saga does not close that window; it makes the window part of the domain model. Two responses are in use:

- **A semantic lock.** The partially-applied entity carries a state marking it in-flight — an order in `PENDING` — and other operations are written to refuse or defer work on entities in that state. The lock is enforced by application logic, not by the storage engine.
- **Readers that tolerate in-flight state.** Queries are defined so that a reserved-but-unpaid order has a correct answer rather than an accidental one.

What does not work is treating the intermediate state as invisible. It is committed data in a live database, and something will read it.

## Orchestration and choreography

The sketch above is **orchestration**: one coordinator holds the sequence and issues the compensations, so the workflow is explicit in a single body of code and its progress is inspectable in one place. The alternative is **choreography**: each service emits events, the next service reacts, and no component holds the sequence. Choreography reduces coupling between the participants, but the workflow then exists only as an emergent property of which services subscribe to which events, so reconstructing it requires distributed tracing rather than reading a function. Newman ties the choice to team boundaries rather than to step count: orchestration suits a saga owned end to end by one team, while choreography suits a workflow spanning several teams, where the reduction in coupling is worth the loss of an explicit sequence.

Where a compensation cannot be expressed cleanly, the decomposition itself is suspect: two steps that cannot be undone independently may belong to the same service, and therefore to the same local transaction.

## Pitfalls

- **Compensations run forward instead of in reverse.** A compensation for an early step deletes the record a later step's compensation needs to identify its own work, and that later compensation fails with a missing-entity error.
- **The compensation loop is not retried.** A single failed compensation aborts the unwind, leaving the saga in neither terminal state — some steps applied, some undone — with no component responsible for finishing it.
- **A step is retried after a timeout without an idempotency key.** The remote transaction had committed; the retry commits a second one, and the customer is charged twice.
- **Compensations are assumed idempotent because forward steps are.** A refund replayed after a redelivered failure message issues a second refund unless the compensation carries its own deduplication key.
- **The step with no available compensation is placed in the middle of the sequence.** Any later failure needs an undo that does not exist, and the saga cannot reach either terminal state.
- **Reads are written against a schema that assumes atomicity.** A report that sums reserved stock counts in-flight orders that will be compensated away, because nothing in the query distinguishes `PENDING` from settled.
- **The saga is treated as though it provided isolation.** Two concurrent sagas both read the same uncommitted intermediate state and each proceeds as if it were final, since no storage-level lock separates them.
