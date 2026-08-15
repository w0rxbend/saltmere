---
title: "Durable Execution with Temporal: Workflows That Survive Crashes"
date: 2026-07-31
track: microservices
summary: "How Temporal turns long-running, failure-tolerant workflows into ordinary code, using event-sourced history and deterministic replay so retries, timeouts, and state survive process crashes."
reading_time: 6
tags: [temporal, durable-execution, microservices, workflows, saga, resilience]
sources:
  - title: "Event History | Temporal Platform Documentation"
    url: "https://docs.temporal.io/encyclopedia/event-history"
  - title: "What is a Temporal Retry Policy? | Temporal Platform Documentation"
    url: "https://docs.temporal.io/encyclopedia/retry-policies"
  - title: "Activity execution — TypeScript SDK | Temporal Platform Documentation"
    url: "https://docs.temporal.io/develop/typescript/activities/execution"
  - title: "Temporal Server Releases | GitHub"
    url: "https://github.com/temporalio/temporal/releases"
  - title: "Saga Orchestration vs Choreography | Temporal Blog"
    url: "https://temporal.io/blog/to-choreograph-or-orchestrate-your-saga-that-is-the-question"
---

**Gist.** A service that charges a card, provisions a resource, and emails a receipt must survive the orchestrating process dying between any two of those steps; the conventional answers — a saga table, a persisted state machine, a queue with dead-letter handling — scatter retry and recovery logic across the codebase. Temporal instead records every orchestration step in a durable append-only **event history** and reconstructs in-memory state by **replaying the orchestration function against that history** after a crash. The cost is a determinism constraint on orchestration code and an additional operational dependency: a Temporal cluster. The open-source server is released on the 1.x line, and software development kits (SDKs) exist for Go, Java, TypeScript, Python, and .NET among other languages.

## The two kinds of function

Temporal partitions application code into two categories governed by different rules.

A **workflow** is the orchestrator: it decides which steps run and in what order. It must be deterministic and performs no input/output (I/O) directly.

An **activity** is an ordinary function performing the side-effecting work — an HTTP request, a database write, a payment-API call. Activities may fail, be slow, and be non-deterministic, because Temporal wraps each one in retries and timeouts.

The workflow never invokes an activity in-process. It emits a **command** to the Temporal Service, which schedules the activity on a worker and records the outcome in history. That indirection is what makes the workflow's execution reproducible: every effect crosses a boundary where it can be logged.

## Replay, and the invariant it depends on

Temporal does not snapshot workflow memory. Every consequential step — activity scheduled, activity completed, timer fired, signal received — is appended to the event history held by the Temporal Service.

When a worker picks up a workflow, whether at first start or after a crash, it **replays the workflow function from its first line** against that history. Each point at which the code would have produced a command is matched against the corresponding recorded event, and **the recorded result is returned without re-executing the side effect**. Once replay consumes the end of history, the reconstructed state equals the state before the crash, and execution proceeds live, emitting new commands.

The invariant this rests on is that **the same input history must drive the code down the same sequence of commands**. A workflow that branches on wall-clock time, a random number, map iteration order, or a direct network call can produce a command sequence diverging from history, and Temporal raises a **non-determinism error** rather than continuing with state that no longer corresponds to the log. This is why side effects are confined to activities, and why the SDKs supply deterministic substitutes for time and randomness. Determinism is the price of not serializing state by hand.

Because state derives from the log rather than from process memory, a workflow may run for seconds or for months, span a worker redeployment, and survive a full crash: killing a worker mid-execution leaves another worker to replay the history and resume. Nothing is lost. Nothing is double-executed **provided activities are idempotent**, because a crash after an activity's side effect but before its completion event is recorded is indistinguishable, from the history's point of view, from an activity that never ran — and it will be retried.

## Retry policies and the two timeouts

Every activity executes under a **retry policy**. The documented defaults are an initial interval of **1 second**, a backoff coefficient of **2.0**, a maximum interval of **100× the initial interval**, and **unlimited maximum attempts**. A flaky downstream dependency is therefore retried with exponential backoff indefinitely unless the policy is tightened; the common adjustment is to bound retries, not to add them.

Timeouts are configured separately from the retry policy, and at least one of them must be set for an activity to be schedulable. **`startToCloseTimeout` bounds a single attempt**; **`scheduleToCloseTimeout` bounds total wall-clock time across all attempts**. The two are not interchangeable: a per-attempt limit alone permits unbounded total duration under an unlimited retry policy.

```typescript
import { proxyActivities } from '@temporalio/workflow';
import type * as activities from './activities';

const { chargeCard } = proxyActivities<typeof activities>({
  startToCloseTimeout: '30 seconds',   // per-attempt limit
  scheduleToCloseTimeout: '10 minutes', // cap across all retries
  retry: {
    initialInterval: '1s',
    backoffCoefficient: 2,
    maximumInterval: '1m',
    maximumAttempts: 5,
    nonRetryableErrorTypes: ['CardDeclined'], // business failures are terminal
  },
});

export async function checkout(orderId: string): Promise<void> {
  await chargeCard(orderId); // retried automatically on transient failure
  // ...provision, notify — each its own durable step
}
```

`nonRetryableErrorTypes` separates the two failure classes: a declined card is a business outcome that no number of retries will change, so the error surfaces to the workflow immediately instead of consuming the retry budget.

### Implementation sketch (Scala)

The load-bearing mechanism is the dispatcher that decides, per command, whether to consult history or to execute. The sketch below models it with a cursor over recorded events; the orchestration function is written once and behaves identically in both modes.

```scala
enum Event:
  case ActivityCompleted(name: String, result: String)

class Replayer(history: Vector[Event], execute: String => String):
  private var cursor = 0

  /** During replay the recorded result is returned and no effect runs;
    * past the end of history the effect runs and a new event is appended. */
  def activity(name: String): String =
    if cursor < history.length then
      history(cursor) match
        case Event.ActivityCompleted(recorded, result) if recorded == name =>
          cursor += 1
          result
        case other =>
          throw IllegalStateException(s"non-determinism: expected $name, got $other")
    else
      val result = execute(name)          // the only side effect in this class
      appendToHistory(Event.ActivityCompleted(name, result))
      cursor += 1
      result

  private def appendToHistory(e: Event): Unit = ??? // durable log write

// The orchestration: identical code path on first run and on every replay.
def checkout(r: Replayer, orderId: String): String =
  r.activity(s"charge:$orderId")
  r.activity(s"provision:$orderId")
  r.activity(s"notify:$orderId")
```

Two properties are visible here. The mismatch branch is the non-determinism error: it fires when the code requests a command the history does not record at that position. And `appendToHistory` occurring **after** `execute` is the reason activities must be idempotent — a crash between those two lines loses the record of an effect that already happened.

## Comparison with a hand-rolled saga

The same guarantees are constructible directly: an orchestrator, a persisted state machine, a retry queue, and compensation steps for rollback. The difference is where the complexity resides. In the hand-rolled version, durability is a per-step obligation — persist after each transition, write the retry loop, and reason explicitly about a crash between step 3 and step 4.

Under Temporal, the retry loop, the persisted state, the timers, and crash recovery belong to the platform, and the saga is written as linear code with `try`/`catch` for compensation, the rollback being activities invoked in the catch block. Idempotency and compensation design remain the author's responsibility; Temporal removes the plumbing, not the distributed-systems reasoning. The counterweight is the determinism constraint on workflow code plus operation of, or subscription to, a Temporal cluster.

## Pitfalls

- **A workflow reading wall-clock time or generating a random number directly** replays down a different branch than the one recorded, and the worker fails the workflow task with a non-determinism error rather than proceeding.
- **Editing deployed workflow code** changes the command sequence for executions already in flight, so in-flight workflows replaying against the old history hit the same non-determinism failure; the change must be gated by the SDK's versioning mechanism.
- **A non-idempotent activity** can charge twice: the side effect completes, the process dies before the completion event is recorded, and the retry re-executes it.
- **Setting `startToCloseTimeout` without `scheduleToCloseTimeout`** bounds each attempt but leaves total duration unbounded, since the default retry policy permits unlimited attempts.
- **Treating a business failure as a transient one** — omitting it from `nonRetryableErrorTypes` — retries a permanently failing call under exponential backoff until the attempt or duration cap is reached, delaying the workflow's handling of an outcome already known at the first attempt.
- **Iterating a hash map inside a workflow** makes the command order depend on unspecified iteration order, producing intermittent non-determinism errors that reproduce only on some replays.
