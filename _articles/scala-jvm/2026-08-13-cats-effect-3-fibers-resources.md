---
title: "Cats Effect 3: fibers, Resource, and a work-stealing runtime for IO"
date: 2026-08-13
track: scala-jvm
summary: "Cats Effect 3 is an effect system built on the IO monad: lazy descriptions of work, lightweight fibers on a work-stealing pool, and Resource for leak-free acquisition. A tour of start/join, Resource.make, and parMapN in Scala 3."
reading_time: 7
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

**Gist.** Side-effecting code that executes the moment it is written cannot be retried, cancelled, or reliably cleaned up, because the effect has already escaped before any combinator can wrap it. Cats Effect (CE) 3 makes an `IO[A]` a **description** of a computation rather than a running one, so the runtime — not the call site — decides when it starts, whether it is interrupted, and which finalisers run; on top of that description it provides fibers as user-space threads and `Resource` as a guaranteed acquire/release pair. The cost is that every effect must be lifted into `IO` and the whole program handed to a runtime at one edge: partial adoption reintroduces exactly the eager effects the model excludes.

The Java Virtual Machine (JVM) now has virtual threads and Scala has direct-style libraries such as Ox, but those address cheap concurrency, not referential transparency. The examples below track the **Cats Effect 3.7.0** release.

```scala
//> using scala 3.7.0
//> using dep org.typelevel::cats-effect::3.7.0
```

## IO: a value composed once, executed once

`IO` is a monad. A large program is built by combining small ones with `flatMap` (or `for`-comprehension sugar), and the result remains a value — passable, storable, re-runnable — until `IOApp` executes it at the program's single entry point.

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

The load-bearing property: **`IO.println` prints nothing when that line is evaluated**; it returns an `IO` that will print when interpreted. Because construction and execution are separated, `hello` can be sequenced twice, wrapped in a timeout, or discarded without any output occurring.

## Fibers: user-space threads with an Outcome

A fiber is a green thread scheduled by the CE runtime rather than the operating system, so its cost is an object allocation and a queue push rather than a kernel thread. `.start` returns a `Fiber` handle; `.join` awaits its **`Outcome`, a three-case algebraic data type: `Succeeded`, `Errored`, `Canceled`**. That third case is the reason `join` returns an `Outcome` rather than an `A`: cancellation is a normal terminal state of a fiber, not an exception, and the caller must decide how to interpret it.

```scala
import cats.effect.IO
import scala.concurrent.duration.*

val work: IO[Int] =
  IO.sleep(500.millis) *> IO.pure(42)

val program: IO[Unit] =
  for
    fib <- work.start          // spawns a fiber, returns immediately
    _   <- IO.println("doing other things...")
    out <- fib.joinWithNever   // await result; a cancelled child never completes
    _   <- IO.println(s"got $out")
  yield ()
```

`joinWithNever` chooses one such interpretation: a cancelled child makes the parent non-terminating, which is preferable to silently observing `Unit` where a result was expected.

Manual `start`/`join` is rarely the right level. The **structured** combinators own the spawn-and-join pair and cancel the remaining fibers as soon as one fails.

```scala
import cats.syntax.all.*

val a: IO[Int] = IO.sleep(300.millis).as(1)
val b: IO[Int] = IO.sleep(200.millis).as(2)

// run both concurrently; if one fails the other is cancelled
val sum: IO[Int] = (a, b).parMapN(_ + _)
```

`parMapN`, `parTraverse` and `race` cover the common cases. The invariant they maintain is that **no fiber they spawned outlives the effect that spawned it** — on success, on error, and on cancellation of the parent. A hand-rolled `start` without a matching `join` or `cancel` breaks that invariant and leaks a running fiber.

## Resource: acquisition paired with release

`Resource` pairs an acquire step with a release step and guarantees the release runs on **all three exits: success, error, and cancellation**. Nested resources release in reverse order of acquisition, so the structure behaves as a stack.

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

`.use` scopes the handle to a block and closes the file as soon as `use` completes, however it completes. `IO.blocking` marks work that parks a thread so the runtime moves it off the compute pool; wrapping the same call in `IO.delay` or `IO.apply` instead leaves it on a compute worker.

## The runtime: one work-stealing pool

CE runs on an integrated **work-stealing** scheduler: a small set of worker threads, each owning a local run queue, taking work from a neighbour's queue when its own is empty. Fibers yield at every asynchronous boundary, which is what allows a handful of OS threads to multiplex a far larger population of fibers. Blocking calls routed through `IO.blocking` are moved off the compute workers onto threads reserved for blocking work, so the compute pool stays runnable.

| Concept        | Written as           | Runtime action                   |
|----------------|----------------------|----------------------------------|
| Spawn          | `.start` / `parMapN` | schedule a fiber on a worker     |
| Await          | `.joinWithNever`     | suspend caller, resume on result |
| Blocking I/O   | `IO.blocking(...)`   | shift to the blocking pool       |
| Cleanup        | `Resource.make`      | guaranteed release on any exit   |

Cancellation, timeouts (`work.timeout(1.second)`) and resource release compose because each is a transformation of a value the runtime controls rather than an instruction already executed. That is the distinction from platform virtual threads, which supply comparably cheap concurrency without referential transparency or a structured release guarantee.

### Implementation sketch (Scala)

A bracket-style scope makes the release invariant explicit. `Resource` supplies this out of the box; the sketch shows the shape it enforces — acquisition is uncancelable, and the finaliser is registered before the body can observe the handle.

```scala
import cats.effect.{IO, Outcome, Resource}

// The guarantee Resource.make encodes, written with the primitive it uses.
def scoped[A, B](acquire: IO[A])(release: A => IO[Unit])(use: A => IO[B]): IO[B] =
  IO.uncancelable { poll =>
    acquire.flatMap { a =>
      // `poll` reopens cancellation for the body only: acquire and release stay atomic.
      poll(use(a)).guaranteeCase {
        case Outcome.Succeeded(_) => release(a)
        case Outcome.Errored(_)   => release(a)
        case Outcome.Canceled()   => release(a)
      }
    }
  }

// Nesting releases in reverse order, because each `use` block encloses the next.
val nested: Resource[IO, (String, String)] =
  for
    outer <- Resource.make(IO.pure("outer"))(_ => IO.println("close outer"))
    inner <- Resource.make(IO.pure("inner"))(_ => IO.println("close inner"))
  yield (outer, inner)
// running `nested.use(IO.println)` prints: close inner, then close outer
```

The `uncancelable`/`poll` pair is the load-bearing part. Without it, a cancellation delivered between `acquire` completing and the finaliser being registered would drop the handle with nothing scheduled to close it.

## Pitfalls

- A fiber started with `.start` and never joined or cancelled keeps running after the effect that spawned it returns; the symptom is work continuing — and log lines appearing — after the enclosing request has responded.
- Escaping the resource handle from `.use` (returning the `BufferedReader` rather than a value read from it) yields a handle that is already closed when the caller touches it; the symptom is an `IOException` on a stream that appeared valid.
- Wrapping a blocking call in `IO.delay` or `IO.apply` rather than `IO.blocking` parks a compute worker; with enough such calls the pool has no runnable thread left and unrelated fibers stop progressing even though the process is idle.
- `join` returns an `Outcome`, so matching only `Succeeded` and `Errored` treats a cancelled child as an unhandled case; `joinWithNever` converts that case into non-termination instead of a silent wrong result.
- Performing a side effect eagerly and then wrapping the finished value in `IO.pure` executes it at construction time; retries and `timeout` then apply to a constant, and the effect happens exactly once regardless of how the `IO` is used.
- A release action that itself throws propagates out of `use`; the `handleError(_ => ())` in the reader example suppresses close failures, which also hides a genuinely failed flush.
