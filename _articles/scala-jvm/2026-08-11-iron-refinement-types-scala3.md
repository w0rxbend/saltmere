---
title: "Iron: Compile-Time Refinement Types for Scala 3"
date: 2026-08-11
track: scala-jvm
summary: "Iron attaches a predicate to a base type so an Int :| Positive is an Int the compiler knows is greater than zero — proven at compile time for literals, validated at runtime for external input via refineEither."
reading_time: 6
tags: [scala3, iron, refinement-types, domain-modeling, type-safety, validation]
sources:
  - title: "Iltotore/iron — Strong type constraints for Scala (GitHub)"
    url: "https://github.com/Iltotore/iron"
  - title: "Iron — Refinement Methods"
    url: "https://iltotore.github.io/iron/docs/reference/refinement.html"
  - title: "Iron — Creating New Types"
    url: "https://iltotore.github.io/iron/docs/reference/newtypes.html"
  - title: "Iron — Constraint reference"
    url: "https://iltotore.github.io/iron/docs/reference/constraint.html"
  - title: "Scaladex — iltotore / iron"
    url: "https://index.scala-lang.org/iltotore/iron"
---

An [opaque type](/articles/scala-jvm/2026-07-31-scala3-opaque-types) gives you a distinct `Temperature` that the compiler refuses to confuse with a bare `Double`. What it does not give you is a guarantee about the *value*: a `Temperature` opaque type still admits `-40.0` unless you hand-write a smart constructor and remember to route every construction through it. **Refinement types** close that gap. They attach a *predicate* to a base type, so the type itself carries the invariant. Iron is the Scala 3 library that makes this practical, and — crucially — proves the predicate at compile time whenever the value is known statically.

Iron is Scala 3 only by design; the whole mechanism rests on `inline`, match types, and compile-time macros that do not exist in Scala 2. The current release is the 3.3.x line (v3.3.1, April 2026), published for Scala 3 across JVM, Scala.js, and Native.

## The core idea: a type plus a predicate

The central operator is `:|` (read "such that"). `Int :| Positive` is a subtype of `Int` refined by the `Positive` constraint — an `Int` the type system knows is greater than zero. Because it is a genuine subtype of `Int`, you can pass it anywhere an `Int` is expected without unwrapping.

```scala
import io.github.iltotore.iron.*
import io.github.iltotore.iron.constraint.numeric.*

val ok: Int :| Greater[0] = 5    // compiles: 5 > 0 is provable at compile time
// val bad: Int :| Greater[0] = -1  // compile error: Could not satisfy Greater[0]

def log(x: Double :| Positive): Double = Math.log(x) // no runtime check needed
```

The second line does not fail at runtime — it fails to *compile*. Iron's automatic refinement inspects the literal `-1` at compile time, finds it cannot satisfy `Greater[0]`, and rejects the program. That is the payoff: illegal literals never reach a running JVM. `Positive` is just a friendlier alias; `Greater[0]` and `Positive` describe the same set.

## Constraints you actually use

Constraints live in packages you import by domain. A representative slice:

| Import | Constraint | Meaning |
|---|---|---|
| `constraint.numeric.*` | `Greater[0]`, `Less[100]` | strict bounds |
| `constraint.numeric.*` | `Positive`, `Negative` | sign |
| `constraint.numeric.*` | `Interval.Closed[1, 150]` | `>= 1 && <= 150` |
| `constraint.string.*` | `Alphanumeric` | letters and digits only |
| `constraint.string.*` | `Match["^[a-z]+$"]` | matches a regex literal |
| `constraint.string.*` | `ValidEmail`, `ValidUUID` | canned patterns |

Constraints compose with `&`, so `Int :| (Greater[0] & Less[100])` is a percentage-like value, and `Interval.Closed[V1, V2]` is itself defined as `GreaterEqual[V1] & LessEqual[V2]` with a human-readable description attached. `Match` takes a string *literal* as a type argument and validates the regex at compile time — the pattern is checked when your code compiles, not on first use.

## Opaque newtypes for domain modeling

Refined aliases are useful, but for domain modeling you usually want a *named* type with its own companion — a value object. Iron's `RefinedType` builds exactly that on top of an opaque type:

```scala
import io.github.iltotore.iron.*
import io.github.iltotore.iron.constraint.numeric.*

type Temperature = Temperature.T
object Temperature extends RefinedType[Double, Positive]

val t = Temperature(15.0)                 // compiles; 15.0 is statically Positive
// val frozen = Temperature(-3.0)         // compile error

val fromRuntime: Either[String, Temperature] = Temperature.either(readSensor())
val maybe: Option[Temperature]               = Temperature.option(readSensor())
val trusted: Temperature                     = Temperature.applyUnsafe(15.0) // throws if invalid
```

`Temperature` is a zero-overhead opaque type: at runtime it *is* a `Double`, no wrapper allocation. But the only way to obtain one is through the companion, and the companion enforces `Positive`. The `apply` method is compile-time checked for literals; `either`, `option`, and `applyUnsafe` handle values that are only known at runtime. This is the "make illegal states unrepresentable" habit, upgraded from "distinct type" to "distinct type that cannot hold an illegal value."

## Validating external input

Compile-time proof works only when the value is a literal or otherwise statically known. Anything crossing a boundary — a JSON field, a query param, a config value — is dynamic, so you *refine* it at runtime and get an error channel back:

```scala
import io.github.iltotore.iron.*
import io.github.iltotore.iron.constraint.all.*

case class Signup(name: String, age: Int)

def parse(rawName: String, rawAge: Int): Either[String, (String :| Alphanumeric, Int :| Interval.Closed[1, 150])] =
  for
    name <- rawName.refineEither[Alphanumeric]
    age  <- rawAge.refineEither[Interval.Closed[1, 150]]
  yield (name, age)
```

`refineEither[C]` returns `Either[String, A :| C]`, with the constraint's description as the `Left`. `refineOption[C]` returns `Option`, and `refineUnsafe` throws — use the latter only when a failure is genuinely a bug. Because the result is a real refined type, everything downstream can *demand* `String :| Alphanumeric` in its signature and never re-check. Validation happens once, at the edge; the invariant then rides the type for the rest of the program.

## Ecosystem integrations

Iron ships thin modules so refined types flow through common libraries without glue. `iron-cats` adds `Validated` accumulation and `NonEmptyList` error reporting; `iron-circe` derives JSON codecs that reject out-of-range values during decoding; `iron-doobie` and `iron-skunk` let refined types map directly to SQL columns; there are also modules for ZIO, jsoniter, pureconfig, ciris, and decline. The pattern is consistent: decode or read into the refined type and the library performs the check at the boundary for you.

## Iron versus refined versus plain opaque types

The older [`refined`](https://github.com/fthomas/refined) library pioneered this style, but it is a Scala 2 project (Shapeless-era machinery) with a Scala 3 port that never became idiomatic. Iron was written for Scala 3 from scratch: it leans on native `inline`/macros, produces clearer compile errors, and integrates with opaque types rather than fighting them. If you are on Scala 3, Iron is the natural choice.

Against plain opaque types, the trade is explicit. Opaque types give you a distinct name and zero-cost erasure but leave *value* validation entirely to your hand-written constructors. Iron keeps the zero-cost erasure, then layers a checkable predicate on top — and proves it at compile time whenever it can. Reach for a bare opaque type when the invariant is "this is a distinct concept"; reach for Iron when the invariant is "and its value must satisfy P."

**Try next:** Define `type Port = Port.T` with `object Port extends RefinedType[Int, Interval.Closed[1, 65535]]`, then try `Port(0)` and `Port(8080)` in a worksheet and confirm only the first is a compile error. Then add `iron-circe`, decode `{"port": 70000}`, and watch the constraint's description surface as a decoding failure instead of a silently accepted bad value.
