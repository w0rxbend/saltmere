---
title: "Cats Effect 3: fibers, Resource, and a work-stealing runtime for IO"
date: 2026-08-13
track: scala-jvm
summary: "Cats Effect 3 is a full effect system built on the IO monad: lazy descriptions of work, millions of lightweight fibers on a work-stealing pool, and Resource for leak-free acquisition. A practical Scala 3 tour with start/join, Resource.make, and parMapN."
reading_time: 6
tags: [scala, cats-effect, io, fibers, concurrency, scala-jvm]
sources:
  - title: "Release v3.7.0 · typelevel/cats-effect (GitHub)"
    url: "https://github.com/typelevel/cats-effect/releases/tag/v3.7.0"
  - title: "Concepts · Cats Effect documentation"
    url: "https://typelevel.org/cats-effect/docs/concepts"
  - title: "Thread Model · Cats Effect documentation"
    url: "https://typelevel.org/cats-effect/docs/thread-model"
  - title: "Schedulers · Cats Effect documentation"
    url: "https://typelevel.org/cats-effect/docs/schedulers"
  - title: "Cats Effect 3: Introduction to Fibers (Rock the JVM)"
    url: "https://rockthejvm.com/articles/cats-effect-3-introduction-to-fibers"
---

The JVM now has virtual threads and Scala has direct-style libraries like Ox, but Cats Effect solves a different problem: it's a *referentially transparent effect system*. Nothing runs until you hand the whole program to a runtime. That single constraint — an `IO[A]` is a **description** of a computation, not a running one — is what buys you resource safety, cancellation, and fearless concurrency. The current stable line is **Cats Effect 3.7.0** (released 8 March 2025), whose big change was porting the work-stealing runtime to Scala Native; on the JVM the integrated runtime has been the default since 3.6.0.

Add it with Scala 3:

```scala
//> using scala 3.7.0
//> using dep org.typelevel::cats-effect::3.7.0
```

## IO: a value you compose, then run once

`IO` is a monad. You build a big program by combining small ones with `flatMap`/`for`, and it stays a value — you can pass it around, retry it, or run it twice — until `IOApp` executes it at the edge of the world.

```scala
import cats.effect.{IO, IOApp}

object Main extends IOApp.Simple:
  val hello: IO[Unit] =
    for
      _ <- IO.println("what's your name?")
      n <- IO.readLine
      _ <- IO.println(s"hi $n")
    yield ()
  def run: IO[Unit] = hello
```

`IO.println` doesn't print when the line executes; it returns an `IO` that *will* print. That's the whole discipline.

## Fibers: cheap, cancellable, structured

A fiber is a green thread scheduled by CE's runtime, not the OS. Starting one is nearly free, so spawning thousands is normal. `.start` gives you a `Fiber` handle; `.join` awaits its `Outcome` (succeeded / errored / cancelled).

```scala
import cats.effect.IO
import scala.concurrent.duration.*

val work: IO[Int] =
  IO.sleep(500.millis) *> IO.pure(42)

val program: IO[Unit] =
  for
    fib <- work.start          // spawns a fiber, returns immediately
    _   <- IO.println("doing other things...")
    out <- fib.joinWithNever   // await result, re-raise cancellation
    _   <- IO.println(s"got $out")
  yield ()
```

You rarely manage fibers by hand, though. The **structured** combinators handle spawn-and-join for you and cancel siblings on the first failure:

```scala
import cats.syntax.all.*

val a: IO[Int] = IO.sleep(300.millis).as(1)
val b: IO[Int] = IO.sleep(200.millis).as(2)

// run both concurrently; if one fails the other is cancelled
val sum: IO[Int] = (a, b).parMapN(_ + _)
```

`parMapN`, `parTraverse`, and `race` are the day-to-day API. They give you concurrency with cancellation and error propagation baked in — no dangling fibers.

## Resource: acquisition that can't leak

`Resource` pairs an acquire step with a release step and guarantees the release runs — on success, error, *or* cancellation. Nest them and they close in reverse order, like a stack.

```scala
import cats.effect.{IO, Resource}
import java.io.{BufferedReader, FileReader}

def reader(path: String): Resource[IO, BufferedReader] =
  Resource.make(
    IO.blocking(BufferedReader(FileReader(path)))   // acquire
  )(r => IO.blocking(r.close()).handleError(_ => ()))  // release, always

val firstLine: IO[String] =
  reader("build.sbt").use(r => IO.blocking(r.readLine()))
```

`.use` scopes the resource to a block; the file is closed the instant `use` completes however it completes. Note `IO.blocking` — it flags work that parks a thread so the runtime shifts it off the compute pool.

## The runtime: one work-stealing pool

CE runs on an integrated **work-stealing** scheduler modeled on Tokio/ForkJoin: a small set of worker threads, each with its own run queue, stealing from neighbours when idle. Fibers yield at every async boundary, so a handful of OS threads multiplex millions of fibers without blocking. Blocking calls go through `IO.blocking` to a separate unbounded pool, keeping the compute workers free.

| Concept        | You write            | Runtime does                     |
|----------------|----------------------|----------------------------------|
| Spawn          | `.start` / `parMapN` | schedule a fiber on a worker     |
| Await          | `.joinWithNever`     | suspend caller, resume on result |
| Blocking I/O   | `IO.blocking(...)`   | shift to the blocking pool       |
| Cleanup        | `Resource.make`      | guaranteed release on any exit   |

The payoff isn't just performance — it's that cancellation, timeouts (`work.timeout(1.second)`), and resource release all compose because everything is a value the runtime controls. That's the line between CE3 and the platform's virtual threads: same cheap concurrency, but with referential transparency and structured cleanup as first-class guarantees.

**Try next:** wrap a real HTTP client or DB connection in `Resource.make`, fan out N requests with `parTraverse`, and add `.timeout` — then kill the app mid-flight and confirm every release ran.
