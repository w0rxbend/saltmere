---
title: "Virtual threads and structured concurrency on the JVM"
date: 2026-07-24
track: scala-jvm
summary: "Virtual threads (JDK 21) made blocking cheap again. Structured concurrency, in its sixth preview as JEP 525 in JDK 26, binds subtask lifetime to a lexical scope so fan-out code cannot leak threads or discard failures."
reading_time: 6
tags: [jvm, virtual-threads, loom, concurrency, java25, java26]
sources:
  - title: "JEP 444: Virtual Threads (final, JDK 21)"
    url: "https://openjdk.org/jeps/444"
  - title: "Structured Concurrency in Java 26 (JEP 525, sixth preview) — deep dive"
    url: "https://javapro.io/2026/06/09/structured-concurrency-in-java-26-jep-525-deep-dive/"
  - title: "Oracle releases Java 26 (March 2026)"
    url: "https://www.oracle.com/news/announcement/oracle-releases-java-26-2026-03-17/"
---

**Gist.** Platform threads on the Java Virtual Machine (JVM) map one-to-one onto operating-system threads, so a request-per-thread server that blocks on input/output (I/O) is bounded by thread count rather than by the work itself; the decade-long workaround was asynchronous or reactive code, which trades that bound for lost stack context and manual lifecycle management. Virtual threads (JEP 444, final in JDK 21) restore blocking code by unmounting a thread from its carrier whenever it would block, and **structured concurrency** (JEP 525, a sixth preview in JDK 26, released March 2026) restores the missing lifecycle discipline by tying every forked subtask to a lexical block. The cost is that both mechanisms are only as good as their invariants: a virtual thread that cannot unmount pins its carrier, and a scope that is not exited normally still has to interrupt and await its children before control leaves the block.

## The scheduling mechanism

A virtual thread is a `Thread` whose execution is scheduled by the JVM rather than by the operating system. It runs by being *mounted* onto a **carrier thread** — a platform thread drawn from a scheduler pool. When the virtual thread executes a blocking operation that the runtime knows how to intercept, its stack is copied out of the carrier and the carrier is released to run some other virtual thread; when the operation completes, the stack is copied back onto some carrier, **not necessarily the same one**. JEP 444 describes the virtual thread's stack as living in the heap as stack chunk objects rather than as a fixed operating-system stack reservation, which is why the number of live virtual threads scales with heap rather than with the process thread limit.

Two consequences follow directly and are easy to get wrong.

**Identity is not affinity.** Because a virtual thread may resume on a different carrier, anything keyed to the carrier's identity is unstable across a blocking call. `ThreadLocal` remains correct, since it is keyed on the virtual thread. Reasoning of the form "this code runs on the same OS thread throughout" does not survive.

**Unmounting is not universal.** JEP 444 documents that a virtual thread is **pinned** to its carrier — it blocks the carrier rather than releasing it — while executing inside a `synchronized` block or method, and while executing a native method or a foreign function. The first of those cases no longer applies on current releases: JEP 491 (JDK 24) associates a monitor with the virtual thread rather than with its carrier, so blocking inside `synchronized` unmounts like any other blocking operation. **Native frames still pin**, so the failure mode survives wherever a blocking call sits below a native or foreign-function frame. A pinned thread that then blocks consumes a carrier for the duration. With a scheduler pool sized to the number of processors, enough concurrently pinned-and-blocked virtual threads exhaust the carriers and progress stops even though no virtual thread has failed. The observable symptom is throughput collapse without CPU utilisation.

## The lifecycle problem cheap threads reintroduce

Once spawning is cheap, fan-out becomes routine, and fan-out with an unstructured executor has no mechanism that couples the fate of siblings. Given an `ExecutorService` and two submitted tasks, if the first fails the second continues to run, and the caller may be parked in the second future's `get()` with the failure already known. Nothing in the program's shape prevents a submitted task from outliving the method that submitted it. The three recurring defects are the **leaked subtask** (nobody joins it and nobody cancels it), the **swallowed failure** (a future whose exception is never retrieved), and the **wasted wait** (blocking on a sibling whose result is already useless).

## The scope invariant

`StructuredTaskScope` binds subtask lifetime to a lexical block. Within the block, `fork` starts a subtask; `join` waits. The invariant the construct enforces is that **control does not leave the block until every forked subtask has completed or been cancelled** — and that holds on normal completion, on exception, and on cancellation of the enclosing thread. The parent-child relationship is therefore explicit rather than incidental, which is what lets cancellation propagate down a subtree: a cancelled request interrupts the subtasks it forked, and those in turn interrupt theirs, instead of leaving downstream calls running with no reader for their results. The same explicit relationship is what allows a thread dump to render a request-to-subtask hierarchy rather than an undifferentiated pool.

```java
// Java 26 preview API: fetch user and orders concurrently, fail fast if either fails
try (var scope = StructuredTaskScope.open()) {
    var user   = scope.fork(() -> fetchUser(id));
    var orders = scope.fork(() -> fetchOrders(id));

    scope.join();                      // wait for both; propagate the first failure

    return new Profile(user.get(), orders.get());
}   // leaving the block guarantees both subtasks are done or cancelled
```

In the JEP 525 form, the policy for how results and failures are combined is supplied as a **joiner** passed to `open()`, rather than expressed by subclassing the scope as the earlier previews required. The no-argument `open()` shown above fails fast: the first subtask failure causes the remaining subtasks to be cancelled and `join` to throw. `Joiner.anySuccessfulOrThrow()` is the other common shape — first success wins, the rest are cancelled. JEP 525 adds `Joiner.onTimeout()`, which makes the reaction to a deadline part of the policy rather than an unconditional exception. The API is still a preview feature, so it compiles and runs only with `--enable-preview`.

### Implementation sketch (Scala)

Structured concurrency is a Java standard-library construct, so Scala 3 calls it directly. Scala has no try-with-resources, so the scope is closed in a `finally`; that close is what enforces the invariant.

```scala
import java.util.concurrent.StructuredTaskScope

final case class Profile(user: User, orders: List[Order])

def loadProfile(id: UserId): Profile =
  val scope = StructuredTaskScope.open[AnyRef]()
  try
    // fork is generic in the subtask's own type, so each handle stays precisely typed
    val user   = scope.fork(() => fetchUser(id))
    val orders = scope.fork(() => fetchOrders(id))

    scope.join()   // fail-fast joiner: first failure cancels the sibling and throws here

    Profile(user.get(), orders.get())   // get() is only legal after join()
  finally scope.close()   // close() is what makes "no subtask outlives the block" true
```

Two details are load-bearing and not decorative. **`get()` before `join()` is a programming error**, not a blocking read: the handle exposes a result only once the scope has been joined. And **`close()` is not a formality** — it is the operation that interrupts unfinished subtasks and waits for them, so a code path that escapes the block without it reintroduces exactly the leak the construct exists to prevent.

Scala libraries model the same parent-child supervision without the preview flag: [Cats Effect](https://typelevel.org/cats-effect/) supervises fibers on its own runtime, and [Ox](https://ox.softwaremill.com/) builds direct-style scopes on virtual threads. Both provide supervised, cancellable concurrency with the guarantee that children do not outlive the parent, expressed in Scala 3 rather than in the Java application programming interface (API).

## Pitfalls

- **Blocking below a native or foreign-function frame pins the carrier.** Throughput falls while CPU utilisation stays low, because carriers are held by virtual threads that have not unmounted; JEP 491 removed the `synchronized` case in JDK 24, but native frames remain.
- **Pooling virtual threads defeats them.** A fixed pool of virtual threads reimposes the bound the mechanism removes; virtual threads are intended to be created per task, not borrowed.
- **`ThreadLocal` at scale is now a memory cost, not a free slot.** The number of copies of a thread-local value tracks the number of live virtual threads, so code that held one copy per pooled thread holds one per in-flight task instead.
- **Carrier identity changes across a blocking call.** Any bookkeeping keyed on the carrier thread rather than on the virtual thread reads as stale or belonging to an unrelated task after a resume.
- **Calling `get()` on a subtask before `join()`** does not wait for the result; it is rejected because the subtask's outcome is not yet established.
- **Escaping the scope block without closing it** leaves subtasks running with no owner, which is the unstructured executor failure mode wearing a structured API.
- **A subtask that ignores interruption cannot be cancelled.** The scope's exit blocks until that subtask finishes, so the enclosing request hangs for as long as the uninterruptible work runs, and the fail-fast policy delivers no benefit.
