---
title: "Scoped Values: ThreadLocal's replacement finally ships in JDK 25"
date: 2026-07-26
track: scala-jvm
summary: "JEP 506 finalized Scoped Values in JDK 25 after five preview rounds. Immutable, bounded, and cheaply inheritable to child threads — everything ThreadLocal isn't when you're running a million virtual threads."
reading_time: 5
tags: [jvm, scoped-values, virtual-threads, loom, concurrency, java25]
sources:
  - title: "JEP 506: Scoped Values"
    url: "https://openjdk.org/jeps/506"
  - title: "JEP 429: Scoped Values (Incubator)"
    url: "https://openjdk.org/jeps/429"
  - title: "JEP 525: Structured Concurrency (Sixth Preview)"
    url: "https://openjdk.org/jeps/525"
  - title: "What's New in Java 25 in 2 Minutes — Inside.java"
    url: "https://inside.java/2025/10/17/new-in-jdk-25-2-mins/"
  - title: "Scoped Values in Java Explained & Compared to ThreadLocal — HappyCoders"
    url: "https://www.happycoders.eu/java/scoped-values/"
---

`ThreadLocal` has been Java's answer to "how do I pass context without threading it through every method signature" since JDK 1.2. It worked because threads were expensive and few. Virtual threads broke that assumption — you can now spawn a million of them for a single request tree — and `ThreadLocal` doesn't degrade gracefully at that scale. **Scoped Values**, finalized as [JEP 506](https://openjdk.org/jeps/506) in JDK 25 (September 2025), are the direct replacement. This is a companion piece to [our structured concurrency article](/2026-07-24-virtual-threads-structured-concurrency) — that one covers the task-lifecycle problem, this one covers the context-propagation problem.

## Why ThreadLocal doesn't fit virtual threads

Three properties of `ThreadLocal` that were fine for a pool of 200 platform threads become liabilities at a million virtual threads:

- **Unbounded mutable state.** Any code with a reference to the `ThreadLocal` can call `.set()` at any point in the thread's life. There's no lexical scope — the value lives until someone calls `.remove()` or the thread dies. Forget the cleanup and you leak.
- **Memory cost per thread.** Every thread carries its own `ThreadLocalMap`. That's negligible for 200 threads; it's real overhead multiplied by a million virtual threads, especially when several libraries in a request path each stash their own contextual value.
- **Expensive, shallow inheritance.** `InheritableThreadLocal` copies the parent's value into every child thread at creation time. Fork ten thousand child tasks from a `StructuredTaskScope` and you've made ten thousand copies of a value that never changes. It also only copies at creation — mutate the parent's value afterward and children don't see it, which is its own source of bugs.

## What Scoped Values do instead

A `ScopedValue` is immutable for the duration it's bound, and the binding has a hard, lexical extent instead of a thread's entire lifetime:

```java
public class RequestContext {
    static final ScopedValue<String> TRACE_ID = ScopedValue.newInstance();

    void handle(String traceId, Request req) {
        ScopedValue.where(TRACE_ID, traceId)
                    .run(() -> processRequest(req));
        // TRACE_ID.get() is unreachable here — the binding is gone
    }

    void processRequest(Request req) {
        logger.info("[{}] handling {}", TRACE_ID.get(), req.path());
        downstreamCall(req);            // TRACE_ID is still bound in callees
    }
}
```

`where(...).run(...)` binds `TRACE_ID` only for the dynamic extent of the lambda — every method called from inside it sees the value, and the instant `run` returns, the binding is gone and eligible for collection. There's no `.set()`, no `.remove()`, no way for a callee to mutate the value out from under its caller. If you need to nest or rebind, `ScopedValue.where(A, a).where(B, b).call(() -> ...)` composes bindings, and `.call()` is the value-returning sibling of `.run()` for a `Callable`-like operation.

## Composing with StructuredTaskScope

The real payoff shows up with structured concurrency. Fork children from inside a bound scope and they inherit the value without a copy — the runtime just makes the parent's binding visible along the call stack that includes the child, rather than duplicating any state:

```java
static final ScopedValue<String> TRACE_ID = ScopedValue.newInstance();

Response handle(Request req) throws Exception {
    return ScopedValue.where(TRACE_ID, newTraceId()).call(() -> {
        try (var scope = StructuredTaskScope.open()) {
            var user   = scope.fork(() -> fetchUser(req.userId()));   // sees TRACE_ID
            var orders = scope.fork(() -> fetchOrders(req.userId())); // sees TRACE_ID

            scope.join();
            return new Response(user.get(), orders.get());
        }
    });
}
```

Both forked tasks can call `TRACE_ID.get()` and see the parent's value, with no copy made per child and no way for one child's work to leak a mutated value to its sibling. When the outer `call` returns, the binding is gone for good — which lines up exactly with the "children can't outlive the scope" guarantee that `StructuredTaskScope` already enforces. Note that `StructuredTaskScope` itself is still a preview API as of JDK 26 — [JEP 525](https://openjdk.org/jeps/525) is its sixth preview round — while Scoped Values, the piece this article covers, are final and require no `--enable-preview` flag from JDK 25 onward.

## ThreadLocal vs. ScopedValue

| | `ThreadLocal` | `ScopedValue` |
|---|---|---|
| Mutability | Mutable via `.set()` anywhere | Immutable once bound |
| Lifetime | Thread's full lifetime, or until `.remove()` | Bound only for the dynamic extent of `run`/`call` |
| Cleanup | Manual (`.remove()`), easy to forget | Automatic — binding ends when the block returns |
| Child-thread inheritance | `InheritableThreadLocal` copies value per child | Shared by reference to forked children, no copy |
| Cost at scale (millions of virtual threads) | Per-thread map plus per-child copy | Bounded, stack-based, GC'd on scope exit |
| Reentrancy / nesting | New `.set()` silently overwrites | New `.where()` binding shadows, then restores |
| API shape | `get()`/`set()`/`remove()` | `ScopedValue.where(...).run(...)`/`.call(...)`/`.get()` |

## The JEP trail, since people always ask

Scoped Values took five rounds to reach final status — worth knowing so you don't cite a stale preview flag in production code:

- JDK 20 — [JEP 429](https://openjdk.org/jeps/429), Incubator
- JDK 21 — JEP 446, Preview
- JDK 22 — JEP 464, Second Preview
- JDK 23 — JEP 481, Third Preview
- JDK 24 — JEP 487, Fourth Preview
- **JDK 25 — [JEP 506](https://openjdk.org/jeps/506), final** — no preview flags needed

That's the same incubate-then-preview-repeatedly pattern virtual threads and structured concurrency followed, and it's worth remembering the two features finalize on different schedules: Scoped Values landed first, structured concurrency is still iterating.

## Migrating in practice

You don't need to rip out every `ThreadLocal` today. The sweet spot for `ScopedValue` is exactly the case `ThreadLocal` handles worst: request-scoped context (trace IDs, tenant IDs, security principals) that's set once near the top of a call tree, read by many layers below, and should never survive past that tree — especially once that tree is a `StructuredTaskScope` fanning out across virtual threads. Long-lived, frequently-reassigned per-thread caches are still a reasonable `ThreadLocal` use case; `ScopedValue` isn't a general-purpose replacement, it's the fix for the specific pattern that breaks under Loom.

**Try next:** find a `ThreadLocal` in your codebase that's set once per request and read downstream — convert it to a `ScopedValue` bound with `where(...).run(...)` at the entry point, then delete whatever cleanup code was calling `.remove()`.
