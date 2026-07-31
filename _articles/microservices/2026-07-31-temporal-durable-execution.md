---
title: "Durable Execution with Temporal: Workflows That Survive Crashes"
date: 2026-07-31
track: microservices
summary: "How Temporal turns long-running, failure-tolerant workflows into ordinary code, using event-sourced history and deterministic replay so retries, timeouts, and state survive process crashes."
reading_time: 5
tags: [temporal, durable-execution, microservices, workflows, saga, resilience]
sources:
  - title: "Event History | Temporal Platform Documentation"
    url: "https://docs.temporal.io/encyclopedia/event-history"
  - title: "What is a Temporal Retry Policy? | Temporal Platform Documentation"
    url: "https://docs.temporal.io/encyclopedia/retry-policies"
  - title: "Activity execution — TypeScript SDK | Temporal Platform Documentation"
    url: "https://docs.temporal.io/develop/typescript/activities/execution"
  - title: "Temporal Server Releases (v1.31.0, Apr 29 2026) | GitHub"
    url: "https://github.com/temporalio/temporal/releases"
  - title: "Saga Orchestration vs Choreography | Temporal Blog"
    url: "https://temporal.io/blog/to-choreograph-or-orchestrate-your-saga-that-is-the-question"
---

A microservice that charges a card, provisions a resource, and emails a receipt has to survive the process dying halfway through. The usual answers — a saga table, a state machine, a queue with dead-letter handling — spread the retry and recovery logic across your codebase. Temporal's pitch is that you write the orchestration as a single ordinary function, and the platform makes it durable. As of mid-2026 the OSS server is at **v1.31.0** (released April 29, 2026), with mature Go, Java, TypeScript, Python, and .NET SDKs.

## Workflows vs. activities

Temporal splits your code into two kinds of function with different rules.

A **workflow** is the orchestrator. It decides what happens and in what order. It must be deterministic and does no I/O directly.

An **activity** is a plain function that does the side-effecting work: an HTTP call, a DB write, a payment API request. Activities can fail, be slow, and be non-deterministic — that's fine, because Temporal wraps each one in automatic retries and timeouts.

The workflow never calls the activity directly. It issues a *command* to the Temporal Service, which schedules the activity on a worker and records the result. This indirection is the whole trick.

## Why workflow code must be deterministic

Temporal doesn't snapshot your workflow's memory. Instead, every meaningful step — activity scheduled, activity completed, timer fired, signal received — is appended to an **event history**, a durable append-only log stored by the Temporal Service.

When a worker picks up a workflow (a fresh start, or a recovery after a crash), it **replays** the workflow function from the beginning against that history. Each line of code that previously produced a command is checked against the recorded event; the recorded result is fed back in without re-executing the side effect. When replay reaches the end of history, the workflow is back in the exact state it was before the crash, and execution continues live.

That only works if the code takes the same path every time. If your workflow branches on `Date.now()`, a random number, iteration order of a map, or a direct network call, replay can diverge from history and Temporal raises a non-determinism error. This is why side effects live in activities and why the SDKs give you deterministic replacements for time and randomness. Determinism is the price of not having to serialize state yourself.

## Durability, concretely

Because state is derived from the log rather than held in memory, a workflow can run for seconds or for six months, span a worker redeploy, and survive a full crash. Kill the worker mid-execution and another worker replays the history and resumes exactly where it left off. Nothing is lost and nothing is double-executed, as long as your activities are idempotent for the retry case.

## Activities: retries and timeouts for free

Every activity runs under a **retry policy**. The defaults do a lot of work: initial interval 1 second, backoff coefficient 2.0, maximum interval 100× the initial interval, and **unlimited** maximum attempts. So a flaky downstream service is retried with exponential backoff indefinitely by default — you tune it down, you rarely add it.

Timeouts are separate and required: `startToCloseTimeout` bounds a single attempt, while `scheduleToCloseTimeout` bounds the total wall-clock time across all retries. Here's a TypeScript workflow calling an activity with an explicit policy:

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
    nonRetryableErrorTypes: ['CardDeclined'], // don't retry business failures
  },
});

export async function checkout(orderId: string): Promise<void> {
  await chargeCard(orderId); // retried automatically on transient failure
  // ...provision, notify — each its own durable step
}
```

Note `nonRetryableErrorTypes`: a declined card is a business outcome, not a transient fault, so you stop retrying and let the workflow handle it.

## Versus hand-rolled sagas

You can build the same guarantees yourself: a saga orchestrator, a persisted state machine, a queue for retries, and compensation steps for rollback. The difference is where the complexity lives. In the hand-rolled version, durability is your responsibility on every step — you persist after each transition, you write the retry loop, you reason about what happens if the orchestrator crashes between step 3 and step 4.

With Temporal, the retry loop, the persisted state, the timers, and the crash recovery are the platform's job. Your saga becomes linear code with `try/catch` for compensation, and rollback is just activities you call in the catch block. You still design idempotency and compensation — Temporal doesn't remove distributed-systems thinking — but it removes the plumbing that usually obscures it. The trade-off is a new operational dependency: you run (or buy) a Temporal cluster, and workflow code has to respect the determinism constraint.

**Try next:** Run `temporal server start-dev` locally, scaffold the TypeScript sample above, then kill the worker process mid-run and restart it — watch the workflow resume from event history instead of starting over.
