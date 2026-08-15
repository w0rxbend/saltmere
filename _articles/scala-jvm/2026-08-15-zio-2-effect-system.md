---
title: "ZIO 2 on Scala 3: Typed Errors, ZLayer, and Fibers That Clean Up"
date: 2026-08-15
track: scala-jvm
summary: "ZIO[R, E, A] is a lazy description of a program with its dependencies and failure modes in the type. How typed errors beat exceptions, how ZLayer wires services with compile-time missing-dependency errors, and how fibers give structured concurrency — with a runnable concurrent-fetch example using timeout and jittered exponential retry."
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

Where Kyo keeps an open type-level effect set (see the kyo-algebraic-effects article) and Ox drops the monad entirely for direct-style code on virtual threads (see the ox-structured-concurrency article), **ZIO** commits to one fixed shape — `ZIO[R, E, A]` — and builds the most complete ecosystem in Scala on top of it: HTTP, streams, config, test, metrics. ZIO 2 (currently **2.1.26**) is a mature bet: less type-system novelty than Kyo, more machinery than Ox, and battle-tested defaults everywhere.

## A program is a value

`ZIO[R, E, A]` reads: *given an environment `R`, run and either fail with `E` or succeed with `A`*. The crucial property is that constructing one **executes nothing**:

```scala
val effect: ZIO[Any, Nothing, Unit] =
  ZIO.succeed(println("hi"))   // nothing printed yet
```

`effect` is a lazy description — an immutable value you can store, pass, compose, and run twice. Only a runtime (usually `ZIOAppDefault`) interprets it. That laziness is what makes everything else composable: `retry` can rerun a description; `race` can start two and interrupt the loser; a test can run the same value against a fake clock. Aliases collapse the common shapes: `UIO[A]` cannot fail, `Task[A]` fails with `Throwable`, `IO[E, A]` needs no environment.

Errors live in the type, and the compiler tracks them the way it tracks return types. `fetch(id): IO[HttpError, User]` tells the caller exactly what can go wrong; `.catchAll`, `.orElse`, or `.mapError` *change the `E` type*, so an effect that has handled everything really is `IO[Nothing, A]` — statically unfailable. Bugs you did not anticipate (the divide-by-zero, the NPE) are kept out of the error channel entirely: they are **defects** (`ZIO.die`), carried on a separate track and surfaced with full fiber traces rather than forcing every signature to admit `Throwable`.

## ZLayer: wiring the compiler checks

`R` is the dependency channel, and `ZLayer[In, E, Out]` is a recipe for building service `Out` from services `In` — memoized, resource-safe (acquisition and release are effects), and buildable in parallel. Services are plain classes; the layer is one line:

```scala
class UserRepo(db: Database):
  def find(id: Long): IO[DbError, Option[User]] = ...

object UserRepo:
  val layer: ZLayer[Database, Nothing, UserRepo] = ZLayer.derive[UserRepo]
```

The payoff is at the edge. `program.provide(UserRepo.layer, Database.live, Config.layer)` asks a macro to assemble the graph, and if a dependency is missing you get a **compile-time error** naming the absent service and suggesting the layer that provides it — not a runtime `NoSuchElementException` from a DI container. SoftwareMill's guide and Pierre Ricadat's write-up converge on the same idiom: constructor injection in plain classes, `ZLayer.derive`/`fromFunction` per service, wire once in `run`.

## Fibers die with their parent

ZIO's concurrency unit is the **fiber** — a virtual thread analogue that predates Loom and carries structured-concurrency rules. `effect.fork` starts a child fiber *supervised by the forking fiber*: if the parent finishes or is interrupted, the child is interrupted too, so fibers cannot silently leak. `forkScoped` attaches the fiber to an enclosing `Scope` instead, for background work that should outlive one function but end with a resource block; `forkDaemon` is the explicit escape hatch.

Mostly you never touch `Fiber` directly:

```scala
a.zipPar(b)        // both in parallel; if one fails, the other is interrupted
a.race(b)          // first winner; loser is interrupted
a.timeout(2.seconds)
ZIO.foreachPar(ids)(fetch).withParallelism(8)
```

All of these are built on **interruption**: a fiber can be stopped between any two operations, and interruption *waits for finalizers* — `acquireRelease` resources are released, `ensuring` blocks run. `timeout` is just `race` against `sleep` plus interruption of the loser, which is why it composes safely around anything, including the retry loop below. Sections that must not be cut short are marked `.uninterruptible`.

## Practical: concurrent fetch, timeout, jittered retry

`Schedule[Env, In, Out]` is a value describing recurrence; `retry` applies one to failures. Schedules compose: `&&` intersects (continue while *both* continue, wait the longer delay), so exponential backoff with an attempt cap is one expression, and `.jittered` multiplies each delay by a random factor (0.8–1.2 by default) to stop clients retrying in lockstep.

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

Run it with `scala-cli run zio-fetch.scala`. Each `fetch` retries independently with capped, jittered exponential backoff; the two run in parallel; the timeout wraps the whole composite, and on expiry *interrupts both branches mid-retry* with finalizers honored. Every policy decision is a value in plain sight — swap `Schedule.exponential` for `Schedule.spaced`, or `&&` in `Schedule.upTo(1.second)`, without touching `fetch`.

The cost of ZIO is its surface area: two extra type parameters on everything, a house style to learn, and an ecosystem that expects you to commit. The return is that failure handling, wiring, and concurrency all live in types the compiler checks — and in descriptions you can test without an HTTP server or a real clock.

**Try next:** replace the inline `fetch` with a `QuoteClient` service behind `ZLayer.derive`, provide a failing test implementation, and use `TestClock.adjust` in zio-test to verify the retry schedule fires exactly five times without sleeping a real millisecond.
