---
title: "Restate: Durable Execution as a Log, Not a Workflow Engine"
date: 2026-08-15
track: sys-patterns
summary: "Restate makes handlers crash-tolerant by journaling every step to an embedded replicated log and replaying the journal on recovery — no external database, no worker polling, a single Rust binary that proxies and records remote procedure calls. Recent releases added flow control and a generally available UI on top of the earlier pause/resume of invocations. The journal model, what virtual objects provide over locks, and where the design differs from Temporal."
reading_time: 7
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

**Gist.** Multi-step business processes — payments, provisioning, agent loops — fail partway through, and the usual repair is hand-written machinery: retry loops, idempotency keys, outbox tables, a `step` column. **Durable execution** engines remove that machinery by recording each completed step in a durable **journal** and, after a crash, replaying the journal so completed steps return their recorded results instead of re-executing. The cost is that every journaled step is a durable write on the critical path, and handler code becomes replay-sensitive: any effect outside the journal happens once per attempt, not once per invocation.

[Temporal](/articles/microservices/2026-07-31-temporal-durable-execution) made the pattern mainstream. **Restate** — at **1.7** as of this writing — keeps the promise and changes the substrate. Rather than a workflow engine with task queues that workers poll, Restate is a **replicated log** placed in front of the services as a remote procedure call (RPC) proxy, journaling everything that passes through it. The consequences are operational: deployment shape, latency, and how keyed state is modelled.

## The journal model

A Restate handler is ordinary code in an ordinary service (TypeScript, Java/Kotlin, Python, Go, or Rust software development kits). Restate receives the invocation, appends it to its log, and **pushes** it to the service over HTTP/2. As the handler runs, every non-deterministic action — a side effect wrapped in `ctx.run`, an RPC to another handler, a sleep, a promise — is recorded in the invocation's journal together with its result:

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

If the process crashes after the charge but before the email, Restate re-invokes the handler and **replays the journal**: `ctx.run("charge card", ...)` returns the recorded `payId` without executing again, and execution resumes at the first step that has no journaled result. The invariant is narrow and worth stating exactly: **each side effect executes at least once, but a step whose result was durably recorded before progress continued is never re-executed**. That is the honest form of "exactly once". A crash *between* the external call and the journal append re-executes that call on recovery — the window is real, and shrinking it is what idempotency keys on the external API are for. Idempotency keys on ingress close the complementary window, returning the original result for a duplicated request, so the observable end-to-end behaviour is effectively-once.

Replay depends on the handler reaching the same sequence of `ctx.*` calls it reached before. Code between journaled steps is re-executed freely on each attempt; **only interactions routed through the context are journaled**, so an unwrapped HTTP call or a write to a local file repeats on every attempt with no record of the earlier one.

## Three service types

Restate exposes three flavours. **Services** are stateless durable functions, as above. **Workflows** add a `run` handler that executes once per key plus signal and query handlers — the Temporal-shaped case. The distinctive one is **virtual objects**: entities addressed by key (`account/user-123`), each carrying its own key/value state and, critically, **single-writer concurrency per key**. The log serializes invocations to a given key, so a virtual object's handlers do not race with themselves.

State reads (`ctx.get`) and writes (`ctx.set`) are journaled with the execution, so **state and progress commit together**. That removes an entire failure class present in the hand-rolled version: there is no interval in which the side effect has happened but the state column has not, because both land in the same journal. It also removes the need for a distributed-lock service, [fencing-token choreography](/articles/sys-patterns/2026-08-11-distributed-locking-fencing-tokens), or `SELECT ... FOR UPDATE`. For naturally keyed domains — accounts, carts, devices, agent sessions — the virtual object replaces both the workflow engine and the locking layer.

For waits on the outside world there are **awakeables**: `ctx.awakeable()` yields an identifier and a durable promise. The identifier is handed to a webhook, a human approver, or a callback queue, and the handler suspends — consuming no function-as-a-service (FaaS) execution time — until something calls the resolve endpoint, minutes or months later.

### Implementation sketch (Scala)

The replay rule is small enough to model directly. This is a sketch of the mechanism, not of the Restate API: an append-only journal of step results, replayed by name.

```scala
final case class Entry(name: String, result: String)

final class Journal(private var entries: Vector[Entry]):
  private var cursor = 0

  /** Returns the recorded result if this step already completed on an
    * earlier attempt; otherwise runs `effect` and appends its result. */
  def step(name: String)(effect: => String): String =
    if cursor < entries.length then
      val e = entries(cursor)
      // divergence means the handler took a different path than it did before
      require(e.name == name, s"replay divergence: expected ${e.name}, got $name")
      cursor += 1
      e.result
    else
      val r = effect                       // crash here and `effect` repeats
      entries = entries :+ Entry(name, r)  // durable append in the real engine
      cursor += 1
      r

def activate(j: Journal, userId: String): Unit =
  val payId = j.step("charge card")(chargeCard(userId))
  j.step("enable account")(enableAccount(userId, payId))
  j.step("send reminder")(sendTrialEmail(userId))
```

The two load-bearing lines are the ordering of `effect` and the append — the source of the at-least-once window — and the `require`, which is where a handler whose control flow changed between attempts is caught rather than silently resuming at the wrong step.

## Restate compared with Temporal

Both replay recorded history to resume interrupted code; the architectures differ. Temporal is a multi-service cluster (frontend, history, matching, worker services) over an external database — Cassandra, MySQL, or Postgres, plus optional Elasticsearch — and workers **poll** task queues for work. Restate is a **single Rust binary** embedding its own replicated log (Bifrost) and RocksDB-based partition state, snapshotting to object storage, and it **pushes** invocations to handlers. The latency argument follows from the shape rather than from any published head-to-head benchmark: a pushed invocation costs one durable append per journaled step, while a polled architecture adds a task-queue round trip and an external-database write to each step.

| | Restate 1.7 | Temporal |
|---|---|---|
| Core abstraction | replicated journal log, RPC proxy | event-sourced workflow history |
| Work distribution | push to service endpoints (HTTP/2, incl. Lambda) | workers long-poll task queues |
| Deployment | one binary; embedded log + RocksDB; S3/GCS/Azure snapshots | 4 services + external DB (+ Elasticsearch) |
| Keyed state | virtual objects, single-writer per key, built-in K/V | model via one-workflow-per-entity |
| Determinism rules | only `ctx.*` journaled; `ctx.run` wraps arbitrary code | full workflow code must be deterministic; sandbox/linters |
| External signals | awakeables (durable promises) | signals |
| Serverless handlers | first-class (suspend/resume) | pollers must run |
| Maturity/ecosystem | 1.x since 2024; smaller ecosystem | public since 2019; large community |

The counterweight is maturity. Temporal's model has years of production hardening at scale, richer tooling, and a larger hiring pool. Restate's determinism surface is smaller — less code has to obey replay rules — but its cluster mode, though generally available, is younger: the recent headline features (pause and resume of invocations, flow control with concurrency limits, the UI reaching general availability) are operational maturity Temporal accumulated earlier.

## Operating it

Self-hosting is one process. `restate-server` provides ingress on port 8080, admin on 9070, and the UI; `restate deployments register http://localhost:9080` points it at an SDK endpoint. The command-line interface and the UI expose per-invocation journal introspection — each journaled step is visible, and an invocation can be paused and later resumed. **Restate Cloud** is the managed alternative where owning the log's disks is undesirable. The mental shift is the one event sourcing asks for: the log is not an implementation detail of the engine; the log is the engine.

A useful verification: run `restate-server` locally, build the subscription service above with the TypeScript SDK, terminate the service process with `kill -9` between two `ctx.run` steps, restart it, and read the invocation's journal in the UI to confirm the first step's result was replayed rather than re-executed.

## Pitfalls

- **An HTTP call not wrapped in `ctx.run` repeats on every attempt.** Only context interactions enter the journal; unwrapped effects leave no record, so recovery re-issues them. The symptom is duplicate charges or duplicate emails after an unrelated crash.
- **A crash between an external call and its journal append re-executes that call.** The recorded-result guarantee begins at the append, not at the call, so the external API must be idempotent for the window to be harmless.
- **Control flow that differs between attempts breaks replay.** Branching on wall-clock time, a random value, or mutable process-local state produces a different sequence of `ctx.*` calls than the journal records, and the invocation fails to resume at the intended step.
- **Single-writer serialization is per key, not per object type.** Two handlers on `account/user-123` are ordered; a handler on `account/user-123` and one on `account/user-456` run concurrently, so an invariant spanning keys is unprotected.
- **Suspended handlers hold no process, but their state persists.** Awakeables and long `ctx.sleep` calls survive restarts, which means an abandoned approval flow remains resumable indefinitely unless the invocation is cancelled.
- **Cluster mode is generally available but young.** Pause/resume and flow control are recent additions; operational practices around them have less accumulated field history than Temporal's equivalents.
