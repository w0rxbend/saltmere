---
title: "ZIO 2 on Scala 3: Typed Errors, ZLayer, and Fibers That Clean Up"
date: 2026-08-15
summary: "ZIO[R, E, A] is a lazy description of a program carrying its dependencies and failure modes in the type. Typed errors compared with exceptions, ZLayer wiring that reports missing dependencies at compile time, and fiber supervision as structured concurrency — with a concurrent-fetch example using timeout and jittered exponential retry."
track: scala-jvm
reading_time: 6
tags: [zio, scala-3, effect-systems, structured-concurrency, functional-programming]
sources:
  - title: "ZIO documentation — zio.dev"
    url: "https://zio.dev/reference/"
  - title: "zio/zio — GitHub releases (v2.1.26)"
    url: "https://github.com/zio/zio/releases"
  - title: "ZIO reference — Schedule (repetition and retrying)"
    url: "https://zio.dev/reference/schedule/"
  - title: "SoftwareMill — Structuring ZIO 2 applications"
    url: "https://softwaremill.com/structuring-zio-2-applications/"
  - title: "Pierre Ricadat — Idiomatic dependency injection for ZIO applications"
    url: "https://blog.pierre-ricadat.com/idiomatic-dependency-injection-for-zio-applications-in-scala/"
---

**Gist.** A Scala program built from exceptions, ad-hoc thread pools and a runtime dependency-injection container hides three things from the type checker: what can fail, what must be supplied, and what must be cancelled. ZIO encodes all three in a single type, `ZIO[R, E, A]`, whose values are **inert descriptions** interpreted by a runtime, so that failure handling, wiring and interruption become ordinary compositional operations. The cost is a fixed three-parameter shape carried by every signature in the codebase and an ecosystem that expects exclusive use.

Where Kyo keeps an open type-level effect set (see the kyo-algebraic-effects article) and Ox drops the monad entirely for direct-style code on virtual threads (see the ox-structured-concurrency article), ZIO commits to one shape and supplies a broad ecosystem on top of it: HTTP, streams, configuration, testing, metrics. The version described here is **2.1.26**.

## A program is a value

`ZIO[R, E, A]` denotes: given an environment `R`, run and either fail with `E` or succeed with `A`. The load-bearing property is that **constructing one executes nothing**.

```scala
val effect: ZIO[Any, Nothing, Unit] =
  ZIO.succeed(println("hi"))   // nothing printed yet
```

`effect` is an immutable value that can be stored, passed, composed and run more than once. Only a runtime — commonly `ZIOAppDefault` — interprets it. **Every downstream combinator depends on that inertness**: `retry` reruns a description, `race` starts two descriptions and interrupts the loser, a test runs the same value against a simulated clock. Aliases collapse the frequent shapes: `UIO[A]` cannot fail, `Task[A]` fails with `Throwable`, `IO[E, A]` requires no environment.

## Two failure channels

Errors occupy the `E` parameter and the compiler tracks them the way it tracks return types. A signature `fetch(id): IO[HttpError, User]` states the complete set of anticipated failures. The handling combinators **change `E`**: `catchAll`, `orElse` and `mapError` rewrite the parameter, so an effect that has handled everything has type `IO[Nothing, A]` and is statically unfailable.

Unanticipated failures — a division by zero, a null dereference — are not admitted into `E`. They are **defects**, introduced by `ZIO.die` or by a thrown exception inside an effect, carried on a separate track and reported with fiber traces. The invariant is that `E` remains the set of failures a caller is expected to reason about, rather than degenerating to `Throwable` at every signature.

## ZLayer: wiring checked by the compiler

`R` is the dependency channel. `ZLayer[In, E, Out]` is a recipe that builds service `Out` from services `In`; layers are **memoized**, **resource-safe** (acquisition and release are themselves effects) and buildable in parallel. Services are plain classes, and the layer is one line.

```scala
class UserRepo(db: Database):
  def find(id: Long): IO[DbError, Option[User]] = ...

object UserRepo:
  val layer: ZLayer[Database, Nothing, UserRepo] = ZLayer.derive[UserRepo]
```

The check happens at the application edge. `program.provide(UserRepo.layer, Database.live, Config.layer)` invokes a macro that assembles the graph; when a dependency is absent the macro emits a **compile-time error naming the missing service and the layer that would provide it**, rather than deferring to a runtime lookup failure in a container. The SoftwareMill guide and Pierre Ricadat's write-up converge on the same idiom: constructor injection into plain classes, one `ZLayer.derive` or `ZLayer.fromFunction` per service, and a single wiring site in `run`.

## Fiber supervision

The concurrency unit is the **fiber**, a lightweight thread analogue with structured-concurrency rules. `effect.fork` starts a child fiber **supervised by the forking fiber**: when the parent completes or is interrupted, the child is interrupted, which is the property that prevents silently leaked background work. `forkScoped` attaches the fiber to an enclosing `Scope` instead, for work outliving one function but bounded by a resource block. `forkDaemon` is the explicit escape from supervision.

Direct use of `Fiber` is rarely needed:

```scala
a.zipPar(b)        // both in parallel; if one fails, the other is interrupted
a.race(b)          // first winner; loser is interrupted
a.timeout(2.seconds)
ZIO.foreachPar(ids)(fetch).withParallelism(8)
```

Each rests on **interruption**: a fiber can be stopped between two operations, and interruption **waits for finalizers** — `acquireRelease` resources are released and `ensuring` blocks run before the fiber is considered complete. `timeout` is a `race` against `sleep` combined with interruption of the loser, which is why it composes around arbitrary effects, including a retry loop. A region that must not be cut short is marked `.uninterruptible`.

### Implementation sketch (Scala)

`Schedule[Env, In, Out]` is a value describing recurrence; `retry` applies one to failures. Schedules compose: `&&` intersects, continuing while **both** operands continue and waiting the longer of the two delays, so capped exponential backoff is a single expression. `.jittered` multiplies each delay by a random factor, drawn by default from the range 0.0–1.0, so that clients that failed together do not retry in lockstep.

```scala
//> using scala 3.7.4
//> using dep dev.zio::zio:2.1.26

import zio.*

case class Quote(source: String, text: String)

def fetch(source: String): IO[String, Quote] =
  Random.nextIntBounded(3).flatMap:
    case 0 => ZIO.fail(s"$source: 503")                       // flaky upstream
    case _ => ZIO.succeed(Quote(source, s"ok from $source")).delay(100.millis)

val policy: Schedule[Any, String, ?] =
  (Schedule.exponential(50.millis) && Schedule.recurs(4)).jittered

object Main extends ZIOAppDefault:
  def run =
    fetch("alpha").retry(policy)
      .zipPar(fetch("beta").retry(policy))                    // concurrent
      .timeoutFail("upstreams too slow")(2.seconds)           // bounds retries too
      .foldZIO(
        err    => Console.printLineError(s"giving up: $err"),
        quotes => Console.printLine(quotes)
      )
```

The file runs under `scala-cli run zio-fetch.scala`. Each `fetch` retries independently under capped, jittered exponential backoff; the two retry loops proceed concurrently; the timeout encloses the composite and **on expiry interrupts both branches mid-retry with finalizers honoured**. Every policy decision is a value in plain sight: substituting `Schedule.spaced` for `Schedule.exponential`, or intersecting `Schedule.upTo(1.second)`, leaves `fetch` untouched.

The cost of the design is its surface area — two extra type parameters on every signature, a distinct house style, and an ecosystem built on the assumption of exclusive use. The return is that failure handling, wiring and concurrency are expressed in types the compiler checks, and in descriptions testable without an HTTP server or a real clock.

**Further work:** replace the inline `fetch` with a `QuoteClient` service behind `ZLayer.derive`, supply a failing test implementation, and use `TestClock.adjust` in zio-test to assert the number of retry firings without elapsing real time.

## Pitfalls

- **`forkDaemon` detaches a fiber from its parent's lifetime**, so a background loop started this way survives the parent's interruption and continues to hold resources until the runtime shuts down; the symptom is work still executing after the request that started it has returned.
- **A wide `.uninterruptible` region defeats `timeout`.** The timeout fires and the loser is signalled, but interruption cannot take effect until the region ends, so the enclosing effect completes later than the stated bound.
- **`Schedule.exponential` without an intersecting bound never stops.** Composing with `Schedule.recurs` or `Schedule.upTo` under `&&` is what supplies termination; a retry policy of `exponential` alone retries a permanently failing upstream indefinitely with growing delays.
- **Throwing inside an effect produces a defect, not a typed failure.** `catchAll` does not observe it, because `catchAll` operates on the `E` channel; the fiber terminates and the exception surfaces through the defect track instead.
- **A layer's memoization is per `provide` call.** Constructing the same layer in two separate provisions yields two instances, so a connection pool intended to be shared can be built twice.
- **`.jittered` randomises delay but not the decision to retry.** Clients that fail simultaneously still all retry; jitter spreads the arrival times within the sampled factor range rather than reducing the total retry load.
