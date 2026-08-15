---
title: "Ox: Direct-Style Structured Concurrency for Scala 3"
date: 2026-08-03
track: scala-jvm
summary: "Ox provides supervised scopes, fork/join, race, timeout and retries for Scala 3 in direct style — sequential-looking code over JDK virtual threads, without an effect type."
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

**Gist.** Concurrency in Scala has conventionally been expressed by lifting every operation into a deferred wrapper — `Future`, `IO`, `ZIO`, `Task` — because blocking a platform thread was expensive enough to be avoided. Project Loom makes blocking a virtual thread cheap, and [Ox](https://github.com/softwaremill/ox) exploits that: concurrency is expressed as blocking `fork`/`join` inside a **supervised scope** that no forked computation may outlive. The cost is that the safety property is scope-shaped rather than type-shaped — the compiler does not track effects, so a leaked `Fork` reference or a value escaping the scope body shows up as runtime behaviour, not as a compile error.

Ox is at version **1.0.6**, targets **Scala 3**, and requires **JDK 21 or later** because it depends on virtual threads. The core artifact is a single dependency:

```scala
//> using dep "com.softwaremill.ox::core:1.0.6"
```

## The scope invariant

A `supervised` block establishes a concurrency scope. Computations are started inside it with `fork`, which returns a `Fork[T]`; `join()` blocks the calling virtual thread until the result is available.

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

There are no callbacks, no `flatMap`, and no runtime to start. The two forks run on separate virtual threads, so the block completes in approximately two seconds rather than three.

The load-bearing property is the **exit invariant**: every fork started inside `supervised` terminates — successfully, exceptionally, or by interruption — before the `supervised` call returns. When the body of the block finishes, forks still running are interrupted, and the scope returns only once all forks have terminated. **A method that opens a scope internally therefore exposes no threading effects to its caller**: on return, no thread it started is still alive.

## Daemon forks, user forks, and the termination condition

Ox distinguishes two kinds of supervised fork, and the distinction determines when the scope is allowed to finish.

- `fork` creates a **daemon** fork. The scope does not wait for it. Once the scope body and all user forks have completed, remaining daemon forks are cancelled.
- `forkUser` creates a **user** fork. The scope waits for it to complete, provided no error occurs elsewhere.

The termination condition is therefore: the scope body has returned **and** every user fork has completed; at that point daemon forks are interrupted and awaited. Work that must finish belongs in `forkUser`; background helpers whose lifetime is bounded by the scope belong in `fork`.

## Error propagation

Supervision means failure is not local. **An exception in any fork ends the entire scope.** Ending the scope interrupts every other running fork, and the original exception surfaces from the `supervised` block.

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
// the second fork fails at 500 ms; the first is interrupted before it
// prints; the exception propagates out of supervised
```

Cancellation is not wired by hand: a failure anywhere tears down the tree, and the teardown completes before control leaves the scope. Where failures are better modelled as typed values than as thrown exceptions, `supervisedError` allows forks to report errors through an `ErrorMode` — for example an `Either` — which the scope treats as a scope-ending failure in the same way as a thrown exception.

## Combinators built on the same machinery

The higher-level operations are scopes internally, so the exit invariant applies to them unchanged.

```scala
import ox.{par, raceSuccess, timeout}
import scala.concurrent.duration.*

// both branches run concurrently; returns when both have finished
val both: (Int, String) = par(computeCount, computeName)

// first success wins; the other branch is cancelled
val fastest: Int = raceSuccess(fromCacheA, fromCacheB)

// TimeoutException if the computation does not finish in time
val bounded: Int = timeout(1.second)(slowLookup)
```

When `raceSuccess` returns, the losing branch has already been interrupted **and awaited**. When `timeout` throws `TimeoutException`, the bounded computation has already been interrupted. No branch continues executing after the combinator has returned.

## Resiliency: retries

Retries are expressed in the same direct style. `retry` takes a `Schedule` — or a fuller `RetryConfig` — and a by-name operation, and re-invokes the operation according to the schedule.

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

`maxInterval` caps the exponentially growing delay; `jitter()` randomises it, which desynchronises clients retrying against the same dependency. `RetryConfig` pairs a schedule with a `ResultPolicy`, so retries can be triggered by an unwanted *result* rather than only by a thrown exception:

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

`retryEither` and `retryWithErrorMode` cover typed-error styles. The same resilience module also provides rate limiters and circuit breakers, usable inline without lifting the surrounding logic into an effect type.

### Implementation sketch (Scala)

A bounded parallel map, expressed with the scope primitives: at most `n` forks exist at any time, and the scope guarantees none survives a failure in any other.

```scala
import ox.{fork, supervised, Fork}
import java.util.concurrent.Semaphore

def parMapBounded[A, B](xs: Vector[A], n: Int)(f: A => B): Vector[B] =
  supervised {
    val permits = Semaphore(n)
    val forks: Vector[Fork[B]] = xs.map { a =>
      permits.acquire()          // blocks the *enclosing* virtual thread,
                                 // so at most n forks are ever outstanding
      fork {
        try f(a)
        finally permits.release()
      }
    }
    forks.map(_.join())
  }
```

If any `f(a)` throws, the scope ends: the remaining forks are interrupted, the `join()` sequence never completes normally, and the exception leaves `parMapBounded`. No fork outlives the call, so the `Semaphore` and any resource `f` holds become unreachable at return.

## Relation to JDK StructuredTaskScope

The JDK's own `StructuredTaskScope` — a preview application programming interface (API) through JDK 25, and reshaped between successive previews — provides the same core shape: fork tasks, join the scope, cancel children with the parent. Ox sits above it with Scala 3 ergonomics — `supervised`/`fork`/`forkUser` in place of scope subclasses and joiners, error propagation and cancellation as defaults rather than configured policies, and combinators (`race`, `par`, `timeout`, `retry`, channels, streaming) that the JDK API does not include. Both run on the same virtual threads.

## Pitfalls

- **A `Fork` that escapes its `supervised` block is already dead.** Returning a `Fork[T]` from the scope body and calling `join()` afterwards fails: the scope interrupted and awaited it before returning.
- **`fork` for work that must complete silently drops it.** Once the scope body and all user forks finish, daemon forks are cancelled mid-flight; the effect is a write or flush that intermittently does not happen. Use `forkUser`.
- **One failing fork cancels siblings that were about to succeed.** The exception from the first failure is what surfaces; sibling work already performed but not yet committed is lost.
- **Interruption is cooperative.** A fork blocked in code that does not respond to `Thread.interrupt` — a native call, or a loop that swallows `InterruptedException` — delays scope exit for as long as it runs, because the scope waits for actual termination.
- **`ResultPolicy.successfulWhen` retries on values, not on exceptions alone.** A predicate that accepts a sentinel such as `0` will treat a failed lookup as success and stop retrying.
- **JDK 20 or earlier will not run Ox.** Virtual threads are required; the failure appears when the classes are loaded, not as a concurrency bug.
