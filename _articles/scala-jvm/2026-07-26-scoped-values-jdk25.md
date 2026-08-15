---
title: "Scoped Values: ThreadLocal's replacement ships in JDK 25"
date: 2026-07-26
track: scala-jvm
summary: "JEP 506 finalized Scoped Values in JDK 25 after an incubator round and four previews. Immutable, lexically bounded, and inherited by forked children without a per-child copy — the properties ThreadLocal lacks under virtual threads."
reading_time: 6
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

**Gist.** Passing request context — a trace identifier, a tenant, a security principal — through every intermediate method signature is impractical, and the mechanism Java has offered since JDK 1.2, `ThreadLocal`, ties that context to a thread's whole lifetime, while its inheritable variant copies the parent's map entries into each child thread. **Scoped Values**, finalized as [JEP 506](https://openjdk.org/jeps/506) in JDK 25, bind an immutable value for the *dynamic extent* of a single call, so the binding is released when that call returns and forked children observe the parent's binding without a copy. The cost is loss of mutability and of ambient reach: a value cannot be reassigned in place, and code outside the bound extent cannot read it at all, so context must be established at a point that dominates every reader.

This article is the companion to [the structured concurrency article](/2026-07-24-virtual-threads-structured-concurrency), which covers task lifecycle; the subject here is context propagation.

## Why ThreadLocal does not fit virtual threads

Three properties of `ThreadLocal` that are tolerable for a pool of a few hundred platform threads become liabilities when a request tree spawns virtual threads by the thousand or million.

- **Unbounded mutable state.** Any code holding a reference to the `ThreadLocal` may call `set` at any point in the thread's life. There is **no lexical bound**: the value survives until `remove` is called or the thread terminates. On a pooled platform thread, which is never terminated, a missed `remove` leaves the value visible to the *next unrelated task* that the pool schedules on that thread.
- **Per-thread memory.** Every thread carries its own `ThreadLocalMap`. The per-thread constant is negligible at a few hundred threads; it multiplies by the number of live virtual threads, and by the number of libraries in the request path that each stash a value.
- **Copying, one-shot inheritance.** `InheritableThreadLocal` copies the parent's map entries into each child **at child-creation time**; the default `childValue` hands the child the same object reference, so what is duplicated is the map, not the object. Forking ten thousand children therefore builds ten thousand maps holding the same reference. Because the copy happens once, a later reassignment in the parent is not observed by children already created — a divergence between parent and child state that no API call reports.

## The binding, and what bounds it

A `ScopedValue` has no setter. The value is supplied to a call, not to a thread:

```java
public class RequestContext {
    static final ScopedValue<String> TRACE_ID = ScopedValue.newInstance();

    void handle(String traceId, Request req) {
        ScopedValue.where(TRACE_ID, traceId)
                   .run(() -> processRequest(req));
        // the binding has ended here; TRACE_ID.get() would throw
    }

    void processRequest(Request req) {
        logger.info("[{}] handling {}", TRACE_ID.get(), req.path());
        downstreamCall(req);            // still inside the extent
    }
}
```

The invariant is that **a binding is visible exactly on the call stack rooted at the `run` or `call` invocation that created it**, and nowhere else. Every method reachable from the lambda observes the value regardless of how deep it sits; the moment the operation returns — normally or by throwing — the binding is removed and the referenced object is again eligible for collection. `get` outside any binding fails rather than returning `null`, so an unestablished context surfaces as an error at the read site instead of a null propagating downstream.

Bindings compose and nest. `ScopedValue.where(A, a).where(B, b).call(() -> ...)` establishes two at once, and `call` is the value-returning sibling of `run`. Rebinding the same `ScopedValue` in an inner extent **shadows** the outer binding for the duration of the inner operation and **restores** it on exit; it does not overwrite it, so the outer frames are unaffected by what inner frames did.

## Composition with StructuredTaskScope

The property that matters at scale appears when children are forked from inside a bound extent. The child's execution is treated as a continuation of the stack that includes the binding, so **the parent's binding is made visible to the child rather than duplicated into it**:

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

Both forked tasks read the parent's value, no per-child copy is made, and because the value is immutable one sibling cannot expose a modified value to another. The lifetime rule aligns with the one `StructuredTaskScope` already enforces: children cannot outlive the scope, and the scope cannot outlive the binding that encloses it.

`StructuredTaskScope` remains a preview application programming interface (API) as of JDK 26 — [JEP 525](https://openjdk.org/jeps/525) is its sixth preview round — whereas Scoped Values are final from JDK 25 and require no `--enable-preview` flag.

## ThreadLocal compared with ScopedValue

| | `ThreadLocal` | `ScopedValue` |
|---|---|---|
| Mutability | Mutable via `set` from anywhere | Immutable once bound |
| Lifetime | Thread's full lifetime, or until `remove` | Dynamic extent of `run`/`call` |
| Cleanup | Manual `remove` | Ends when the operation returns |
| Child inheritance | `InheritableThreadLocal` copies per child | Parent binding visible, no copy |
| Cost across many virtual threads | Per-thread map plus a map copy per child | One binding shared by the children, released on extent exit |
| Nesting | A later `set` overwrites | Inner `where` shadows, then restores |
| Read with no value | Returns `null` (or the initial value) | Fails at the call site |

## The standardization trail

Scoped Values reached final status after an incubator round and four previews:

- JDK 20 — [JEP 429](https://openjdk.org/jeps/429), Incubator
- JDK 21 — JEP 446, Preview
- JDK 22 — JEP 464, Second Preview
- JDK 23 — JEP 481, Third Preview
- JDK 24 — JEP 487, Fourth Preview
- **JDK 25 — [JEP 506](https://openjdk.org/jeps/506), final** — no preview flag required

Virtual threads and structured concurrency followed the same incubate-then-preview sequence, but the three features finalize on independent schedules: Scoped Values are final while structured concurrency is still in preview.

### Implementation sketch (Scala)

Scala 3 calls the Java API directly. The load-bearing detail is that the binding is a *parameter of a call*, so a helper that wraps `where(...).call(...)` is the only place a value is ever supplied:

```scala
object RequestContext:
  val TraceId: ScopedValue[String] = ScopedValue.newInstance()

  /** Establishes the binding for exactly the extent of `body`. */
  def withTrace[A](id: String)(body: => A): A =
    ScopedValue.where(TraceId, id).call(() => body)

  /** Fails if called outside any binding — no null to propagate. */
  def currentTrace: String = TraceId.get()

def handle(req: Request): Response =
  RequestContext.withTrace(newTraceId()):
    val scope = StructuredTaskScope.open[Any]()
    try
      val user   = scope.fork(() => fetchUser(req.userId))    // observes the binding, no copy
      val orders = scope.fork(() => fetchOrders(req.userId))
      scope.join()
      Response(user.get(), orders.get())
    finally scope.close()
```

A by-name parameter is used deliberately: evaluating `body` eagerly would run it **before** the binding exists, and `currentTrace` inside it would then fail.

## Migrating in practice

The pattern `ScopedValue` fits is the one `ThreadLocal` handles worst: context established once at the top of a call tree, read by many layers below, and required not to survive that tree — in particular when the tree fans out through a `StructuredTaskScope`. A long-lived, frequently reassigned per-thread cache is not that pattern, and `ThreadLocal` remains applicable there. `ScopedValue` is not a general replacement for `ThreadLocal`; it addresses the subset of uses whose cost and correctness degrade under virtual threads.

## Pitfalls

- **Calling `get` outside any binding throws rather than returning a default.** Code moved out of the request path — a background refresh, a shutdown hook, a retry scheduled onto an unrelated executor — leaves the extent and fails at the read.
- **Escaping the extent with a captured lambda.** Submitting work to an executor that outlives the `run`/`call` invocation means the task executes after the binding was removed; the value it needs is gone even though the closure was created inside the extent.
- **Expecting an inner rebinding to be seen by outer frames.** An inner `where` shadows only for its own operation; on return the outer binding is restored, and any value computed inside is not observable through the `ScopedValue` afterwards.
- **Eager evaluation in a Scala wrapper.** A helper taking `body: A` instead of `body: => A` evaluates the operation before `call` is entered, so reads inside it occur outside the binding.
- **Leaving `--enable-preview` in the build because of `StructuredTaskScope`.** Scoped Values need no flag from JDK 25; the flag is required only by the preview APIs used alongside them, and it opts the entire compilation into preview, not the one class that needs it.
- **Migrating a pooled-thread `ThreadLocal` incrementally.** Until the last `set` is removed, a stale value can still be observed by a subsequent task on the same pooled platform thread, and the `ScopedValue` read path will not report the discrepancy.
