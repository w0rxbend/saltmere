---
title: "Structured concurrency in JDK 25 (JEP 505): treat a group of tasks as one unit"
date: 2026-07-31
track: scala-jvm
summary: "Virtual threads made spawning a thread per task cheap, but a leaked or orphaned subtask is still a bug. JEP 505 — the fifth preview in JDK 25 — reworks StructuredTaskScope around a static open() factory and reusable Joiners so a group of concurrent tasks lives and dies as a single lexical unit, with error and cancellation handled for you."
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

Virtual threads (final since JDK 21) removed the cost objection to "one thread per task" — you can spawn a million. But cheap threads don't make *correct* concurrency. If you fan out three subtasks and one throws, do the other two get cancelled, or do they run on as orphans consuming resources? If the caller is interrupted, does that propagate down? With bare `ExecutorService` + `Future`, the answer is "only if you wrote a lot of careful try/finally," and most people didn't. Structured concurrency fixes the *shape* of the problem: a group of concurrent subtasks should have the same block structure as a group of sequential statements — enter together, leave together, errors and cancellation flowing along the call tree.

JEP 505 is the **fifth preview** of this API, shipped in **JDK 25** (September 2025). It's a notable API revision over earlier previews, so if you tried `StructuredTaskScope` before, the surface has changed.

## The new shape: open() + a Joiner

You no longer subclass or `new` a scope. You call the static **`StructuredTaskScope.open()`**, passing a **`Joiner`** that encodes your completion policy. Inside the try-with-resources block you `fork()` subtasks; each returns a `Subtask<T>`. Then `join()` blocks until the Joiner's policy is satisfied and returns whatever that Joiner produces.

**"All must succeed"** — the fan-out/gather case. `Joiner.allSuccessfulOrThrow()` returns a stream of the completed subtasks; if any subtask fails, `join()` throws and the rest are cancelled:

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

If `fetchUser(7)` throws, the scope **cancels the still-running forks**, `join()` throws a `FailedException` wrapping the cause, and you never leak a thread. There is no path out of the try block that leaves a subtask alive — that guarantee is the whole point.

**"First success wins"** — the redundant-request / hedging case:

```java
String fastest(List<URI> mirrors) throws Exception {
    try (var scope = StructuredTaskScope.open(
            Joiner.<String>anySuccessfulResultOrThrow())) {
        mirrors.forEach(m -> scope.fork(() -> download(m)));
        return scope.join();     // returns the first successful result,
    }                            // cancels the losers automatically
}
```

Other built-in Joiners cover the rest: `awaitAllSuccessfulOrThrow()` (wait for all, no results collected), `awaitAll()` (wait for every subtask regardless of outcome), and `allUntil(Predicate)` (collect until your predicate says stop). You can implement the `Joiner` interface yourself for custom short-circuiting.

## What you get that raw executors don't

- **No orphans.** The try-with-resources close is a hard barrier: control cannot leave the block while a subtask runs. Structured, not leaked.
- **Error short-circuiting.** One failure cancels siblings by policy — you don't wire up the propagation.
- **Cancellation flows down the tree.** Interrupt the owner thread and the interruption propagates into the forked subtasks; they aren't left running against a caller that's already gone.
- **Observability.** Because the scopes nest, a thread dump shows the *tree* of concurrent work — parent scope, its subtasks, their child scopes — instead of a flat soup of anonymous pool threads. Debugging concurrency by reading a structured dump is a genuine quality-of-life jump.

## Caveats before you lean on it

It's a **preview feature**: compile and run with `--enable-preview --release 25`, and the API may shift again (indeed JDK 26 carries a sixth preview, JEP 525). Don't hard-wire it into a library's public signatures yet. It also lives in `java.util.concurrent`, and it's built on virtual threads — each `fork` runs on one — so it targets I/O-bound fan-out, not CPU-bound parallel compute (that's still `parallelStream`/ForkJoin territory). For Scala users the same discipline exists today in cats-effect and ZIO via supervised fibers; JEP 505 brings the guarantee to plain Java and to any JVM code that spawns threads directly.

**Try next:** find a spot in your code that does `executor.submit(...)` three times and then `future.get()` in a loop, and rewrite it with `StructuredTaskScope.open(Joiner.allSuccessfulOrThrow())`. Then throw an exception from one subtask and confirm — with a log line in each — that the *others stop* instead of running to completion. That automatic sibling-cancellation is what you were previously getting wrong by hand.
