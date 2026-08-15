---
title: "Kyo: Algebraic Effects for Scala 3 Without the Monad Stack"
date: 2026-08-14
track: scala-jvm
summary: "Kyo tracks effects in one open type-level set carried by a single `<` pending type, so error handling, dependency injection and IO are added where required instead of stacked as monad transformers. A concrete Scala example with Env, Abort and Sync — plus direct style."
reading_time: 5
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

**Gist.** Established Scala effect systems fix the shape of a computation in advance: `ZIO[R, E, A]` provides exactly one environment channel and one error channel, while cats-effect's `IO[A]` provides neither, so additional capabilities arrive as hand-stacked monad transformers (`EitherT`, `ReaderT`, `StateT`), each layer contributing wrapping, allocation and a `mapK` to reconcile types. **Kyo** replaces the fixed shape with a single pending type `A < S`, where `S` is an **open, unordered, type-level set of effects** that widens as capabilities are used and shrinks as handlers discharge them. The cost is a library that is still in **release-candidate status on the path to 1.0.0**, with effect names that have moved between candidates, and a type-level encoding whose inference errors surface as set-valued types rather than single missing instances.

## One type, an open set of effects

The core type `A < S` is read "`A` pending `S`". `A` is the value produced; `S` is the set of effects that must be discharged before the computation can run. The set is encoded as an intersection type, so effects accumulate with `&`:

```scala
val x: Int < (Env[Config] & Abort[String] & Sync)
```

The computation yields an `Int` once a `Config` has been supplied, the possible `String` failure has been dealt with, and the `Sync` side effects have been executed.

Two properties do the work. First, **`map` and `flatMap` are the same operation**: applying a function `A => (B < S2)` to a value of type `A < S` produces `B < (S & S2)`. Union of the two effect sets is the only bookkeeping, so composition never requires lifting one layer through another, and **plain values lift automatically** — there is no `pure` wrapper.

Second, the set is **unordered**. With transformers the nesting order is part of the type: `EitherT[StateT[IO, S, *], E, A]` and `StateT[EitherT[IO, E, *], S, A]` are distinct types with distinct semantics for state surviving a failure, and converting between them is manual work. In Kyo, adding a capability widens the set and nothing else. Where an effect carries state that cannot be shared unchanged across concurrent branches, the interaction is settled at compile time rather than left as a latent race: forking a computation that still carries a state effect such as `Var` (mutable state) under `Async` (concurrency) requires the state effect to declare how it is isolated across the fork.

## Introduce, use, discharge

Every effect follows the same three-phase lifecycle:

1. **Introduce.** A constructor such as `Env.get`, `Abort.get` or `Sync.defer` produces a value whose pending set contains that effect.
2. **Use.** The value flows through ordinary `for`-comprehension binding; the pending set is the union of everything touched.
3. **Discharge.** A handler removes **exactly one effect** from the set and, where the effect has a result shape, reifies it in the value type — `Abort.run[String]` turns `Int < (Abort[String] & S)` into `Result[String, Int] < S`.

The invariant that makes the model predictable: **a program can only be executed when its pending set is empty or contains only effects the runtime itself can discharge.** A pure computation ends with `.eval`; a computation still pending `Sync` is run at the program edge by `KyoApp.run`. Forgetting a handler is therefore a compile-time type mismatch, not a runtime failure.

### Implementation sketch (Scala)

```scala
//> using dep io.getkyo::kyo-core:1.0.0-RC1   // any 1.0 release candidate

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
```

`.handle(...)` threads the computation through a pipeline of handlers applied left to right. After `Env.run` supplies the configuration and `Abort.run` collapses the failure channel into a `Result[String, Int]`, the only remaining member of the set is `Sync`, which `KyoApp.run` executes. **Handlers are ordinary values**, so they can be constructed, passed as parameters and reused across programs.

## Direct style

The monadic form is the primitive; the `kyo-direct` module adds an imperative-looking surface through a macro. Inside `direct { ... }`, `.now` extracts the value of a pending computation, and the block is desugared back into the same `flatMap` chain:

```scala
import kyo.*

val program2: Int < (Env[Config] & Abort[String] & Sync) =
  direct:
    val cfg  = Env.get[Config].now
    val line = Sync.defer("41").now
    Abort.get(line.toIntOption.toRight("not a number")).now + cfg.retries
```

The type and the semantics are identical to `program`. **`.now` marks the sequencing point**, so the location of each suspension remains visible in the source rather than being implied by a bare `flatMap`.

## Consequences of the encoding

A signature enumerates exactly the capabilities a function touches: a pure helper is `A < Any`, a function that logs is pending `Sync`, a validator is pending `Abort[E]`. Callers read that set directly from the type without a transformer layer per capability. Kyo's implementation relies on Scala 3 inlining and opaque types to keep this expressiveness from translating into the per-layer boxing that transformer stacks incur; no published benchmark in the cited sources separates it from a hand-tuned `IO` under load, so the claim here is limited to the mechanism, not to a measured margin.

## Pitfalls

- **Pinning to a floating release candidate breaks compilation across upgrades.** Effect names have been renamed on the path to 1.0 — `IO` became `Sync`, `Resource` became `Scope` — so a build that resolves a newer RC fails on unresolved names rather than degrading gracefully.
- **A missing handler manifests as an effect left in the pending set, not as a missing implicit.** The compiler reports a mismatch between the expected type and one that still contains, for example, `Env[Config]`; the fix is an added entry in the `.handle` pipeline, not an implicit import.
- **Handler order in `.handle` determines the nesting of the reified results.** `Abort.run` applied before another result-producing handler yields a differently nested `Result`, so the final value type changes even though the pending set ends up empty either way.
- **Forking a `Var`-carrying computation under `Async` is not accepted on its own.** Kyo requires the state effect to say how it is isolated across the fork, so code that wants state genuinely shared between concurrent branches needs a concurrency-safe effect rather than `Var`.
- **Values lift implicitly, which hides an omitted effect at the call site.** A plain `A` is accepted wherever `A < S` is expected, so a helper that was meant to perform a side effect but returns a constant type-checks silently.
