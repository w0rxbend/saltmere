---
title: "Ox: Direct-Style Structured Concurrency for Scala 3"
date: 2026-08-03
track: scala-jvm
summary: "SoftwareMill's Ox brings supervised scopes, fork/join, race, and retries to Scala 3 in plain direct style — sequential-looking code on top of JDK virtual threads, no monads or effect system required."
reading_time: 6
tags: [scala3, ox, structured-concurrency, virtual-threads, project-loom, resiliency]
sources:
  - title: "softwaremill/ox — Safe direct-style streaming, concurrency and resiliency for Scala on the JVM"
    url: "https://github.com/softwaremill/ox"
  - title: "Fork & join threads — Ox documentation"
    url: "https://ox.softwaremill.com/stable/structured-concurrency/fork-join.html"
  - title: "A tour of Ox — Ox documentation"
    url: "https://ox.softwaremill.com/stable/tour.html"
  - title: "Retries — Ox documentation"
    url: "https://ox.softwaremill.com/latest/scheduling/retries.html"
  - title: "Safe direct-style Scala: Ox 0.1.0 released — SoftwareMill"
    url: "https://softwaremill.com/safe-direct-style-scala-ox-0-1-0-released/"
---

For a decade the Scala answer to concurrency was "wrap it in an effect": `Future`, then `IO`, `ZIO`, `Task`. You paid for composability with a monadic wrapper on every operation, `for`-comprehensions to sequence them, and a mental model where nothing runs until the runtime interprets your program. Project Loom changed the arithmetic. Once the JVM can cheaply block a virtual thread, the reason to defer everything into a monad largely evaporates — you can write straight-line, blocking-looking code and let millions of virtual threads carry the concurrency. [Ox](https://github.com/softwaremill/ox), from SoftwareMill, is the Scala 3 library built for exactly that world: safe, direct-style concurrency and resiliency with no `IO` type in sight.

As of this writing Ox is at **1.0.6**, targets **Scala 3**, and requires **JDK 21+** because it leans directly on virtual threads (Project Loom). One dependency gets you the core:

```scala
//> using dep "com.softwaremill.ox::core:1.0.6"
```

## Supervised scopes and forks

The heart of Ox is the `supervised` scope. Inside it you start concurrent computations with `fork`, and the scope guarantees they never outlive it. `fork` returns a `Fork[T]` whose `join()` blocks the current (virtual) thread until the result is ready.

```scala
import ox.{fork, supervised, sleep}
import scala.concurrent.duration.*

val (a, b) = supervised {
  val f1 = fork {
    sleep(2.seconds)
    1
  }
  val f2 = fork {
    sleep(1.second)
    2
  }
  (f1.join(), f2.join())
}
```

No callbacks, no flatMap, no runtime to launch. The code reads top-to-bottom and blocks where you'd expect — but `f1` and `f2` run on separate virtual threads concurrently, so the block completes in roughly two seconds, not three.

The structural guarantee is the whole point. Every fork started inside `supervised` will finish successfully, with an exception, or via interruption before the scope returns. When the block completes, any fork still running is interrupted, and the scope only returns once all forks have actually terminated. A method that opens a scope never leaks threads: there are no threading effects visible to its caller.

## fork vs forkUser, and how errors propagate

Ox distinguishes two kinds of supervised fork. `fork` creates a **daemon** fork: the scope won't wait for it just because it's still running — once the main body and all user forks are done, remaining daemon forks are cancelled. `forkUser` creates a **user** fork: the scope will wait for it to complete (assuming no errors elsewhere). Use `forkUser` for work that must finish, `fork` for background helpers whose lifetime is bounded by the scope.

Error propagation is automatic and is where the "supervised" name earns its keep. An exception in *any* fork ends the entire scope; ending the scope cancels every other running fork by interruption, and the exception surfaces from the `supervised` block:

```scala
supervised {
  forkUser {
    sleep(1.second)
    println("Hello!")
  }
  forkUser {
    sleep(500.millis)
    throw new RuntimeException("boom!")
  }
}
// the "boom!" fork fails at 500ms; the other fork is interrupted
// before it can print; the exception propagates out of supervised
```

You don't wire up cancellation by hand — a failure anywhere tears down the whole tree deterministically. If you'd rather model failures as typed values instead of thrown exceptions, `supervisedError` lets forks report errors through an `ErrorMode` (for example an `Either`) that the scope aggregates.

## race, par, and timeout

On top of forks, Ox ships the combinators you actually reach for. `par` runs computations in parallel and returns all results once every branch succeeds; `raceSuccess` returns the first successful result and interrupts the losers; `timeout` bounds a computation:

```scala
import ox.{par, raceSuccess, timeout}
import scala.concurrent.duration.*

// both run concurrently; returns when both finish
val both: (Int, String) = par(computeCount, computeName)

// first success wins, the other is cancelled
val fastest: Int = raceSuccess(fromCacheA, fromCacheB)

// TimeoutException if it doesn't finish in time
val bounded: Int = timeout(1.second)(slowLookup)
```

Each of these is built on the same scope machinery, so the structured-concurrency guarantees hold: when `raceSuccess` returns, the losing branch has already been interrupted and awaited before you get control back. Nothing keeps running behind your back.

## Resiliency: retries

Resiliency lives in the same direct style. `retry` takes a `Schedule` (or a fuller `RetryConfig`) and a by-name operation, and simply re-invokes it according to the schedule:

```scala
import ox.resilience.retry
import ox.scheduling.Schedule
import scala.concurrent.duration.*

val result =
  retry(
    Schedule.exponentialBackoff(100.millis)
      .maxRetries(4)
      .jitter()
      .maxInterval(5.minutes)
  )(callFlakyService())
```

For finer control, `RetryConfig` pairs a schedule with a `ResultPolicy` so you can retry not just on thrown exceptions but on unwanted results:

```scala
import ox.resilience.{retry, RetryConfig, ResultPolicy}
import ox.scheduling.Schedule

val n = retry(
  RetryConfig[Throwable, Int](
    Schedule.immediate.maxRetries(3),
    ResultPolicy.successfulWhen(_ > 0)
  )
)(directOperation)
```

Variants like `retryEither` and `retryWithErrorMode` cover typed-error styles. The same resilience module also provides rate limiters and circuit breakers, all usable inline without lifting your logic into an effect.

## How this differs from JDK StructuredTaskScope

The JDK's own `StructuredTaskScope` (finalized in JDK 25) gives you the same core idea — fork tasks, join the scope, cancel children with the parent. Ox sits a layer above it with Scala-3-native ergonomics: `supervised`/`fork`/`forkUser` instead of scope subclasses and joiners, error propagation and cancellation as defaults rather than opt-in policies, and a batteries-included set of combinators (`race`, `par`, `timeout`, `retry`, channels, streaming) that the JDK API deliberately leaves out. If you want the raw platform primitive, reach for `StructuredTaskScope`; if you want an opinionated, direct-style concurrency and resiliency toolkit for a Scala 3 codebase, Ox is the higher-level answer — and it runs on the very same virtual threads.

**Try next:** add `com.softwaremill.ox::core:1.0.6` to a scala-cli script on JDK 21+, wrap two `fork`s in a `supervised` block, then make one of them throw and watch the other get cancelled.
