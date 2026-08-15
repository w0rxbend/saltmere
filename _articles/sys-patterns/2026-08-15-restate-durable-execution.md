---
title: "Restate: Durable Execution as a Log, Not a Workflow Engine"
date: 2026-08-15
track: sys-patterns
summary: "Restate makes handlers crash-proof by journaling every step to an embedded replicated log and replaying the journal on recovery — no external database, no worker polling, just a single Rust binary that proxies and records your RPCs. Version 1.7 (July 2026) added flow control and a GA UI on top of the 1.6 line's pause/resume and object-storage snapshots. How the journal model works, what virtual objects buy you over locks, and where it genuinely differs from Temporal."
reading_time: 6
tags: [durable-execution, restate, workflow, distributed-systems, event-log]
sources:
  - title: "Durable Execution — Restate documentation"
    url: "https://docs.restate.dev/concepts/durable_execution/"
  - title: "Building a modern Durable Execution Engine from First Principles — Restate blog"
    url: "https://www.restate.dev/blog/building-a-modern-durable-execution-engine-from-first-principles"
  - title: "Restate vs. Temporal — restate.dev"
    url: "https://restate.dev/vs/temporal"
  - title: "Restate v1.7.0 release notes"
    url: "https://github.com/restatedev/restate/blob/main/release-notes/v1.7.0.md"
  - title: "9 Best Temporal Alternatives for Durable Execution — ZenML blog"
    url: "https://www.zenml.io/blog/temporal-alternatives"
---

Every team that wires together payments, provisioning, or multi-step AI agents eventually reinvents the same machinery: retry loops, idempotency keys, outbox tables, a state column named `step`. **Durable execution** engines exist to delete that machinery, and [Temporal](/articles/microservices/2026-07-31-temporal-durable-execution) made the pattern mainstream. **Restate** — at **1.7** as of July 2026, after 1.6.0 landed January 30, 2026 — takes the same promise and rebuilds the substrate: instead of a workflow *engine* with task queues that workers poll, Restate is a **replicated log** that sits in front of your services as an RPC proxy, journaling everything that flows through it. The distinction sounds academic. Operationally, it changes almost everything: deployment shape, latency, and how you model state.

## The journal model

A Restate handler is ordinary code in your ordinary service (TypeScript, Java/Kotlin, Python, Go, or Rust SDKs). Restate receives the invocation, appends it to its log, and *pushes* it to your service over HTTP/2. As the handler runs, every non-deterministic action — a side effect wrapped in `ctx.run`, an RPC to another handler, a sleep, a promise — is recorded in the invocation's **journal** together with its result:

```typescript
import * as restate from "@restatedev/restate-sdk";

const subscription = restate.service({
  name: "subscription",
  handlers: {
    activate: async (ctx: restate.Context, req: { userId: string }) => {
      // journaled side effect: runs once, result replayed on retry
      const payId = await ctx.run("charge card", () =>
        chargeCard(req.userId),
      );
      // durable RPC to a virtual object, keyed by userId
      await ctx.objectClient(account, req.userId).enable(payId);
      // durable timer: suspends, survives restarts, costs nothing on FaaS
      await ctx.sleep({ days: 14 });
      await ctx.run("send reminder", () => sendTrialEmail(req.userId));
    },
  },
});
```

If the process crashes after the charge but before the email, Restate re-invokes the handler and **replays the journal**: `ctx.run("charge card", ...)` returns the recorded `payId` without executing again, and execution resumes at the first step with no journaled result. That's the exactly-once framing done honestly — each side effect executes *at least once*, but because its result is durably recorded before progress continues, a completed step is never re-executed. Combine that with idempotency keys on ingress (duplicate requests get the original result) and the observable behavior is effectively-once end to end.

## Three service types, one big idea

Restate exposes three flavors. **Services** are stateless durable functions, as above. **Workflows** add a `run` handler that executes exactly once per key plus signal/query handlers — the Temporal-shaped use case. The distinctive one is **virtual objects**: entities addressed by key (`account/user-123`), each carrying its own K/V state and — the crucial property — **single-writer concurrency per key**. Restate's log serializes invocations to a given key, so a virtual object's handlers never race with themselves. State reads (`ctx.get`) and writes (`ctx.set`) are journaled with the execution, so state and progress commit together — no distributed-lock service, no [fencing-token choreography](/articles/sys-patterns/2026-08-11-distributed-locking-fencing-tokens), no `SELECT ... FOR UPDATE`. For anything naturally keyed — accounts, carts, devices, agent sessions — this replaces both the workflow engine *and* the locking layer.

For waits on the outside world there are **awakeables**: `ctx.awakeable()` yields an ID and a durable promise; you hand the ID to a webhook, human approver, or callback queue, and the handler suspends — off the FaaS bill entirely — until something calls the resolve endpoint, minutes or months later.

## Restate vs. Temporal

Both replay recorded history to resume interrupted code; the architectures underneath differ sharply. Temporal is a multi-service cluster (frontend, history, matching, worker services) over an external database — Cassandra, MySQL, or Postgres, plus optional Elasticsearch — and your workers *poll* task queues for work. Restate is a **single Rust binary** embedding its own replicated log (Bifrost) and RocksDB-based partition state, snapshotting to object storage; it *pushes* invocations to your handlers. Fewer hops shows up in latency — Restate advertises p99 under 170ms for a 10-step workflow on a multi-AZ cluster, a regime where per-step round-trips through a task queue and database add up.

| | Restate 1.7 | Temporal |
|---|---|---|
| Core abstraction | replicated journal log, RPC proxy | event-sourced workflow history |
| Work distribution | push to service endpoints (HTTP/2, incl. Lambda) | workers long-poll task queues |
| Deployment | one binary; embedded log + RocksDB; S3/GCS/Azure snapshots | 4 services + external DB (+ Elasticsearch) |
| Keyed state | virtual objects, single-writer per key, built-in K/V | model via one-workflow-per-entity |
| Determinism rules | only `ctx.*` journaled; `ctx.run` wraps arbitrary code | full workflow code must be deterministic; sandbox/linters |
| External signals | awakeables (durable promises) | signals |
| Serverless handlers | first-class (suspend/resume) | awkward (pollers must run) |
| Maturity/ecosystem | 1.x since 2024; smaller ecosystem | production since ~2019 at large scale; huge community |

The honest counterweight: Temporal's model has years of production hardening at enormous scale, richer tooling, and a bigger hiring pool. Restate's determinism story is easier to get right (there's less "workflow code" that must obey special rules), but its cluster mode, while GA, is simply younger — 1.6/1.7's headline features (pause/resume of invocations, partition-memory balancing, flow control with concurrency limits, the UI hitting 1.0) are the kind of operational maturity Temporal grew years ago.

## Running it

Self-hosting is genuinely one process: `restate-server` gives you ingress on 8080, admin on 9070, and the UI; `restate deployments register http://localhost:9080` points it at your SDK endpoint, and the CLI plus UI give you per-invocation journal introspection — you can watch each journaled step, and since 1.6, restart an invocation from a journal prefix. **Restate Cloud** is the managed alternative when you'd rather not own the log's disks. Either way, the mental shift is the same one event-sourcing asked of you: the log is not an implementation detail of the engine — the log *is* the engine.

**Try next:** run `restate-server` locally, build the subscription service above with the TypeScript SDK, `kill -9` the service process mid-handler between two `ctx.run` steps, restart it, and read the invocation's journal in the UI to confirm the first step's result was replayed, not re-executed.
