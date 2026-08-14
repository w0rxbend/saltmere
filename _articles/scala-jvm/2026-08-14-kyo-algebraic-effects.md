---
title: "Kyo: Algebraic Effects for Scala 3 Without the Monad Stack"
date: 2026-08-14
track: scala-jvm
summary: "Kyo tracks effects in one open type-level set with a single `<` pending type, so you add error handling, dependency injection, and IO where you need them instead of stacking monad transformers. A concrete Scala 3 example with Env, Abort, and Sync — plus direct style."
reading_time: 6
tags: [kyo, algebraic-effects, scala-3, effect-systems, functional-programming]
sources:
  - title: "Kyo documentation (Overview) — getkyo.io"
    url: "https://getkyo.io/latest/"
  - title: "getkyo/kyo — GitHub repository"
    url: "https://github.com/getkyo/kyo"
  - title: "getkyo/kyo — GitHub releases"
    url: "https://github.com/getkyo/kyo/releases"
  - title: "Comparing effect systems in Scala: Kyo, Gears, and Ox — VirtusLab"
    url: "https://virtuslab.com/blog/scala/comparing-effect-systems-in-scala-kyo-gears-and-ox"
---

ZIO and cats-effect both hardcode a fixed shape. `ZIO[R, E, A]` bakes in exactly one environment channel and one error channel; cats-effect's `IO[A]` gives you neither, so you reach for `EitherT`, `ReaderT`, `StateT` and stack monad transformers by hand — each layer adding wrapping, allocation, and a `.mapK` somewhere to make the types line up. **Kyo** (getkyo.io; **1.0.0-RC6** is the current release candidate as of August 2026, following the last 0.x line, 0.19.0) asks a different question, as VirtusLab put it: what if we didn't hardcode the error and environment channels, and just added them where we need them?

## One type, an open set of effects

Kyo's core type is `A < S`, read "`A` pending `S`". `A` is the value the computation produces; `S` is an open, type-level *set* of effects it still needs before it can run. That set is an intersection type, so effects accumulate by `&`:

```scala
val x: Int < (Env[Config] & Abort[String] & Sync)
```

This returns an `Int` once the config is supplied, the possible `String` failure is dealt with, and the `Sync` side effects are executed. There is no ordering to negotiate and no transformer to stack — adding a new effect just widens the set. Crucially, `map` and `flatMap` are the same operation: a function `A => (B < S2)` applied to `A < S` yields `B < (S & S2)`. Plain values lift automatically, so there is no `pure(...)` ceremony.

This is *effect widening* instead of *monad stacking*. Where transformers force you to pick an order (`EitherT[StateT[IO, ...]]` is a different type from `StateT[EitherT[IO, ...]]`), Kyo's set is unordered and commutative where it is safe to be. It even statically outlaws combinations that cannot safely commute — pairing `Var` (state) with `Async` (concurrency) is a type error, not a lurking race.

## Introduce, use, discharge

Every effect follows the same three-phase lifecycle. You *introduce* it (`Env.get`, `Abort.get`, `Sync.defer`), *use* the value, then *discharge* it with a handler that removes exactly one effect from the set:

```scala
//> using scala 3.7.4
//> using dep io.getkyo::kyo-core:1.0.0-RC6

import kyo.*

case class Config(retries: Int)

// Needs config (Env), can fail (Abort), performs a side effect (Sync)
val program: Int < (Env[Config] & Abort[String] & Sync) =
  for
    cfg  <- Env.get[Config]
    line <- Sync.defer("41")                    // stand-in for real IO
    n    <- Abort.get(line.toIntOption.toRight("not a number"))
  yield n + cfg.retries

object Main extends KyoApp:
  run {
    program.handle(
      Env.run(Config(retries = 1)),   // discharge Env[Config]
      Abort.run[String]               // discharge Abort -> Result[String, Int]
    )
  }
  // prints: Success(42)
```

`.handle(...)` threads the computation through a pipeline of handlers left to right. After `Env.run` supplies the config and `Abort.run` collapses the failure channel into a `Result[String, Int]`, only `Sync` remains — and `KyoApp.run` executes that at the program's edge. Handlers are ordinary values, so you can build them, pass them around, and reuse them. If the set were empty (a pure computation) you would finish with `.eval` instead of a runtime.

## Direct style when the for-comprehension gets noisy

The monadic form is the foundation, but `kyo-direct` adds an imperative-looking syntax via a macro. Inside `direct { ... }`, `.now` extracts the value of any pending computation, and the block desugars back into the same `flatMap` chain:

```scala
import kyo.*

val program2: Int < (Env[Config] & Abort[String] & Sync) =
  direct:
    val cfg  = Env.get[Config].now
    val line = Sync.defer("41").now
    Abort.get(line.toIntOption.toRight("not a number")).now + cfg.retries
```

Same type, same semantics — you just read it top to bottom like plain code. The `.now` boundary is where the effect is sequenced, so it stays explicit where suspension happens rather than hiding it behind a bare `flatMap`.

## Why the design matters

The payoff is precision and cost. A function's signature lists *exactly* the capabilities it touches — a pure helper is `A < Any`, a logger needs `Sync`, a validator needs `Abort[E]` — and callers see that in the type without a monad transformer tax. Kyo leans hard on Scala 3 features (inlining, opaque types, aggressive allocation avoidance) so that this expressiveness does not translate into the per-layer boxing that transformer stacks incur; performance parity with hand-tuned `IO` is an explicit goal, not an afterthought.

It is still a release candidate, so pin a version and read the migration notes between RCs — the effect names have moved (`IO` became `Sync`, `Resource` became `Scope`) on the road to 1.0. But the model is stable: one pending type, an open effect set, handlers that peel effects off one at a time. If you have ever fought a `mapK` to make two transformer stacks agree, that fight simply does not exist here.

**Try next:** add a `Var[Int]` counter to `program` to track attempts, discharge it with `Var.run(0)`, and watch the pending set shrink one handler at a time as you add `Var.run` to the `.handle` pipeline.
