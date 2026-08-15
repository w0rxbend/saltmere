---
title: "Structured concurrency in JDK 25 (JEP 505): a group of tasks as one unit"
date: 2026-07-31
track: scala-jvm
summary: "Virtual threads made a thread per task cheap, but a leaked or orphaned subtask remains a bug. JEP 505 — the fifth preview, shipped in JDK 25 — reworks StructuredTaskScope around a static open() factory and reusable Joiners, so a group of concurrent tasks lives and dies as a single lexical unit with error propagation and cancellation defined by policy."
reading_time: 5
tags: [java, jdk25, structured-concurrency, virtual-threads, loom, jep505]
sources:
  - title: "JEP 505: Structured Concurrency (Fifth Preview) — openjdk.org"
    url: "https://openjdk.org/jeps/505"
  - title: "Inside.java — JEP 505 targeted to JDK 25"
    url: "https://inside.java/2025/05/12/jep505-target-jdk25/"
  - title: "Rock the JVM — Structured Concurrency in JDK 25: What's New"
    url: "https://rockthejvm.com/articles/structured-concurrency-jdk-25"
  - title: "InfoQ — JEP 505: Structured Concurrency (Fifth Preview)"
    url: "https://www.infoq.com/news/2025/05/jep-505-concurrency-preview-5"
  - title: "JDK 25 release (java.util.concurrent.StructuredTaskScope)"
    url: "https://openjdk.org/projects/jdk/25/"
---

**Gist.** Virtual threads (final since JDK 21) removed the cost objection to one thread per task, but a fan-out built from `ExecutorService` and `Future` has no structural rule forcing subtasks to end before their initiator returns: a sibling failure leaves orphans running, and interrupting the caller does not reach them. Structured concurrency imposes the block structure of sequential statements on concurrent subtasks — **subtasks are forked inside a lexical scope and cannot outlive it** — with a `Joiner` object encoding the completion policy. The cost is a rigid shape: the fan-out must be expressible as a lexical block, and in JDK 25 the API is still a **preview feature**, so it requires `--enable-preview` and is subject to further revision.

## The invariant

The property structured concurrency enforces is containment. Control cannot leave the scope's block while any forked subtask is still running, because the scope is an `AutoCloseable` and its `close()` is a barrier: at the closing brace **every subtask is either completed or cancelled**. There is no exit path — normal return, exception, or interruption — that escapes the block with live children. Every guarantee below is a consequence of that one invariant rather than an independent feature.

## The shape in JDK 25: open() plus a Joiner

JEP 505 is the **fifth preview** of the API and ships in **JDK 25** (September 2025). It is a notable revision of the earlier previews; code written against them does not carry over unchanged. A scope is no longer subclassed or constructed with `new`. It is obtained from the static factory **`StructuredTaskScope.open()`**, which takes a **`Joiner`** describing when `join()` may return and what it produces. Inside the try-with-resources block, `fork()` submits work and returns a `Subtask<T>` handle; `join()` blocks until the Joiner's policy is satisfied.

**All must succeed** — the fan-out/gather case. `Joiner.allSuccessfulOrThrow()` yields a stream of completed subtasks, and any subtask failure makes `join()` throw while the remaining forks are cancelled:

```java
import java.util.concurrent.StructuredTaskScope;
import java.util.concurrent.StructuredTaskScope.*;

List<User> loadAll(List<Long> ids) throws Exception {
    try (var scope = StructuredTaskScope.open(
            Joiner.<User>allSuccessfulOrThrow())) {

        ids.forEach(id -> scope.fork(() -> fetchUser(id)));  // each on a virtual thread

        return scope.join()          // waits for all; throws if any failed
                    .map(Subtask::get)
                    .toList();
    }   // scope closes here: every subtask is guaranteed done or cancelled
}
```

If `fetchUser(7)` throws, the scope **cancels the still-running forks** and `join()` throws a `FailedException` wrapping the original cause.

**First success wins** — the redundant-request or hedging case:

```java
String fastest(List<URI> mirrors) throws Exception {
    try (var scope = StructuredTaskScope.open(
            Joiner.<String>anySuccessfulResultOrThrow())) {
        mirrors.forEach(m -> scope.fork(() -> download(m)));
        return scope.join();     // returns the first successful result,
    }                            // cancels the losers automatically
}
```

The remaining built-in policies cover the other common shapes: `awaitAllSuccessfulOrThrow()` waits for all subtasks without collecting results, `awaitAll()` waits for every subtask regardless of outcome, and `allUntil(Predicate)` collects until the supplied predicate reports that enough has arrived. The `Joiner` interface can also be implemented directly for a custom short-circuiting rule.

## What the scope provides that a raw executor does not

- **No orphans.** Closing the block is a hard barrier rather than a convention, so a subtask cannot survive the method that forked it.
- **Error short-circuiting by policy.** A single failure cancels its siblings according to the Joiner in force; the propagation is not hand-wired in `try`/`finally`.
- **Cancellation flows down the tree.** Interrupting the owner thread propagates into the forked subtasks, so they are not left executing on behalf of a caller that has already left.
- **Observability.** Because scopes nest, a thread dump shows the tree of concurrent work — a parent scope, its subtasks, and their child scopes — rather than a flat set of anonymous pool threads.

### Implementation sketch (Scala)

Scala 3 calls the same `java.util.concurrent` types directly. The load-bearing detail is that `fork` accepts a `Callable`, satisfied by a Scala lambda through single-abstract-method conversion, and that the `Joiner` — not the call site — decides the failure semantics:

```scala
import java.util.concurrent.StructuredTaskScope
import java.util.concurrent.StructuredTaskScope.{Joiner, Subtask}
import scala.jdk.CollectionConverters.*

def loadAll(ids: List[Long]): List[User] =
  val scope = StructuredTaskScope.open(Joiner.allSuccessfulOrThrow[User]())
  try
    ids.foreach(id => scope.fork(() => fetchUser(id)))
    scope.join().map[User]((s: Subtask[User]) => s.get()).toList().asScala.toList
  finally scope.close()          // the barrier: no subtask outlives this call

// Hedged read: the first mirror to answer wins, the rest are cancelled.
def fastest(mirrors: List[URI]): String =
  val scope = StructuredTaskScope.open(Joiner.anySuccessfulResultOrThrow[String]())
  try
    mirrors.foreach(m => scope.fork(() => download(m)))
    scope.join()
  finally scope.close()
```

The `finally scope.close()` is what a Java try-with-resources emits; writing it explicitly makes the invariant visible. Removing it does not merely leak a scope object — it removes the containment guarantee, at which point the code is an `ExecutorService` fan-out with extra syntax.

## Constraints before adoption

The API is a **preview feature** in JDK 25: compilation requires `--enable-preview --release 25` and execution requires `--enable-preview`, and JEP 505 states that the API may change again in a future release. Preview types are therefore a poor choice for a library's public signatures. Each `fork` runs on a virtual thread, which orients the construct toward I/O-bound fan-out; CPU-bound parallel computation remains the domain of `parallelStream` and ForkJoin. Equivalent discipline already exists for Scala code in cats-effect and ZIO through supervised fibers; JEP 505 extends the guarantee to plain Java and to any JVM code that spawns threads directly.

## Pitfalls

- **Compiling without `--enable-preview`** fails outright, and a class compiled with preview features refuses to load on a different JDK version — preview class files are pinned to the JDK version that compiled them, so a JDK 25 build artefact will not run on a later JDK.
- **Migrating code written against an earlier preview** does not compile: JEP 505 replaced scope subclassing and construction with the static `open()` plus `Joiner` pair, so `new StructuredTaskScope.ShutdownOnFailure()` has no direct successor.
- **Calling `Subtask::get` before `join()` returns** is a misuse; the result is only defined once the Joiner's policy has been satisfied.
- **Forking from outside the scope's own block** — for example, handing the scope to another method that stores it — defeats containment, because the lexical barrier only constrains code that sits inside the block.
- **Expecting sibling cancellation under `awaitAll()`**: that Joiner waits for every subtask regardless of outcome, so one failure leaves the others running to completion rather than short-circuiting.
- **Using the construct for CPU-bound work** gives up the work-stealing scheduling that `ForkJoinPool` applies to compute-heavy task trees; a virtual thread executing a tight loop occupies its carrier thread for the duration, so forking many more subtasks than cores adds no throughput.
