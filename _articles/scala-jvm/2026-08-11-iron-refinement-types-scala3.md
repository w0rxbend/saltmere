---
title: "Iron: Compile-Time Refinement Types for Scala 3"
date: 2026-08-11
track: scala-jvm
summary: "Iron attaches a predicate to a base type so that an Int :| Positive is an Int the compiler knows is greater than zero — proven at compile time for statically known values, validated at runtime for external input via refineEither."
reading_time: 7
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

**Gist.** An [opaque type](/articles/scala-jvm/2026-07-31-scala3-opaque-types) makes `Temperature` distinct from a bare `Double` but says nothing about the *value*: `-40.0` remains admissible unless every construction site is routed through a hand-written smart constructor. **Refinement types** attach a predicate to the base type, so the type itself carries the invariant, and Iron discharges that predicate **at compile time whenever the value is statically known**. The cost is that compile-time proof covers only statically known values; everything arriving from outside the program must still be refined at runtime through an explicit error channel, and the constraint machinery runs in the compiler, so it is paid in compilation time rather than at execution.

Iron targets Scala 3 exclusively. The mechanism rests on `inline` definitions and compile-time macros, which have no Scala 2 equivalent. Scaladex lists the artifacts as published for Scala 3 across the Java Virtual Machine (JVM), Scala.js, and Scala Native.

## The core operator: a type plus a predicate

The central operator is `:|`, read "such that". `Int :| Positive` denotes an `Int` refined by the `Positive` constraint. **The refined type is a genuine subtype of the base type**, so a value of `Int :| Positive` is accepted anywhere an `Int` is expected, with no unwrapping step and no wrapper allocation.

```scala
import io.github.iltotore.iron.*
import io.github.iltotore.iron.constraint.numeric.*

val ok: Int :| Greater[0] = 5    // compiles: 5 > 0 is provable at compile time
// val bad: Int :| Greater[0] = -1  // compile error: Could not satisfy Greater[0]

def log(x: Double :| Positive): Double = Math.log(x) // no runtime check needed
```

The commented line fails to *compile* rather than failing at runtime. Iron's automatic refinement inspects the literal `-1` during compilation, determines that it cannot satisfy `Greater[0]`, and rejects the program. **A literal that violates a constraint therefore never reaches a running JVM.** `Positive` is an alias: `Greater[0]` and `Positive` describe the same set of values.

## Constraint families

Constraints are grouped into packages imported by domain. A representative slice:

| Import | Constraint | Meaning |
|---|---|---|
| `constraint.numeric.*` | `Greater[0]`, `Less[100]` | strict bounds |
| `constraint.numeric.*` | `Positive`, `Negative` | sign |
| `constraint.numeric.*` | `Interval.Closed[1, 150]` | `>= 1 && <= 150` |
| `constraint.string.*` | `Alphanumeric` | letters and digits only |
| `constraint.string.*` | `Match["^[a-z]+$"]` | matches a regex literal |
| `constraint.string.*` | `ValidUUID` | a well-formed UUID string |

Constraints compose with `&`: `Int :| (Greater[0] & Less[100])` describes a percentage-like value. `Interval.Closed[V1, V2]` is itself defined as `GreaterEqual[V1] & LessEqual[V2]` with a human-readable description attached. **`Match` takes a string *literal* as its type argument**, which is what allows a statically known string to be checked against the pattern during compilation rather than at execution.

## Named refined types for domain modelling

Refined aliases suffice for local signatures, but a domain value object needs a *named* type with its own companion. Iron's `RefinedType` constructs one on top of an opaque type:

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

`Temperature` erases to `Double` at runtime, so no wrapper object is allocated. **The companion is the sole construction path, and it enforces `Positive` on every path through it.** The four entry points differ only in how they report failure: `apply` is compile-time checked and admits statically known values; `either` yields `Either[String, Temperature]`; `option` discards the message; `applyUnsafe` throws. The result upgrades the "make illegal states unrepresentable" discipline from a distinct type to a distinct type that cannot hold an illegal value.

## Validating external input

Compile-time proof applies only where the value is a literal or otherwise statically known. Any value crossing a boundary — a JSON field, a query parameter, a configuration entry — is dynamic and must be refined at runtime, which returns an error channel:

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

`refineEither[C]` returns `Either[String, A :| C]`, using **the constraint's description as the `Left` value**. `refineOption[C]` returns an `Option`, discarding the description; `refineUnsafe` throws, and is appropriate only where a violation is a program defect rather than bad input. Because the success case is a real refined type, every downstream signature can demand `String :| Alphanumeric` and perform no further check. **The check occurs once, at the boundary; the invariant then travels in the type.**

Note the sequencing above: the `for` comprehension over `Either` is fail-fast, so a bad `rawName` short-circuits and the `rawAge` violation is never reported.

### Implementation sketch (Scala)

The load-bearing idea is that the refined type is the base type plus evidence, and that the boundary is the only place evidence is manufactured. The sketch below shows a parser whose internal stages cannot re-admit an invalid value, and accumulates both field errors instead of stopping at the first.

```scala
import io.github.iltotore.iron.*
import io.github.iltotore.iron.constraint.all.*

type Port = Port.T
object Port extends RefinedType[Int, Interval.Closed[1, 65535]]

type Host = Host.T
object Host extends RefinedType[String, Not[Empty]]

final case class Endpoint(host: Host, port: Port)

// Boundary: untyped input in, evidence out. Errors accumulate rather than short-circuit.
def parseEndpoint(rawHost: String, rawPort: Int): Either[List[String], Endpoint] =
  (Host.either(rawHost), Port.either(rawPort)) match
    case (Right(h), Right(p)) => Right(Endpoint(h, p))
    case (h, p)               => Left(List(h, p).collect { case Left(msg) => msg })

// Interior: no validation, because the types already carry it.
def render(e: Endpoint): String = s"${e.host}:${e.port}"

val configured: Endpoint = Endpoint(Host("localhost"), Port(8080)) // both proven at compile time
```

Two properties matter. `render` takes an `Endpoint` and performs no bounds check, because `Port` cannot exist outside `[1, 65535]`. The literal construction on the last line compiles without a runtime branch; replacing `8080` with `70000` turns it into a compilation error rather than a defect discovered in production.

## Ecosystem integrations

Iron ships thin modules so that refined types pass through common libraries without adapter code. `iron-cats` supplies `Validated` accumulation and `NonEmptyList` error reporting; `iron-circe` derives JSON codecs that reject out-of-range values during decoding; `iron-doobie` and `iron-skunk` map refined types onto SQL columns; further modules exist for ZIO, jsoniter, pureconfig, ciris, and decline. The pattern is uniform: decoding or reading into the refined type causes the library to perform the constraint check at the boundary.

## Iron, refined, and plain opaque types

The older [`refined`](https://github.com/fthomas/refined) library established this style on Scala 2, where the encoding depends on Scala 2's implicit and macro machinery. Iron was written for Scala 3 from the outset on `inline` and Scala 3 macros, and builds its named types on opaque types. No published benchmark separates the compile-time cost of the two.

Against plain opaque types the trade is explicit. An opaque type supplies a distinct name and erases at zero cost, but leaves *value* validation to hand-written constructors. Iron retains the erasure and adds a checkable predicate, discharged statically where the value permits. A bare opaque type suits an invariant of the form "this is a distinct concept"; Iron suits "and its value must satisfy P".

## Pitfalls

- **`refineUnsafe` and `applyUnsafe` throw at runtime.** Applied to values decoded from external input, they convert a recoverable validation failure into an exception on the request path.
- **A `for` comprehension over `Either` is fail-fast.** Chaining several `refineEither` calls reports only the first violated field; accumulating every field error requires `Validated` from `iron-cats` or an explicit match on the results.
- **Compile-time proof requires a statically known value.** Passing a `val` whose type is a plain `Int` where `Int :| Positive` is expected fails to compile even when the runtime value is positive, because the evidence is absent; refinement must be applied where the value enters the program.
- **`Match` requires a literal type argument.** A pattern held in a runtime `String` cannot be supplied as the type parameter, so patterns loaded from configuration fall outside compile-time checking entirely.
- **Refined types erase to their base type.** A `Temperature` is a `Double` at runtime, so a pattern match on `Any` or a reflective check cannot distinguish it from any other `Double`, and the invariant does not survive erasure-dependent code paths.
- **The constraint is on the value, not on the arithmetic.** `Int :| Positive` values added together produce a plain `Int`; the result must be refined again if the positivity is to be carried forward, and overflow is not excluded by the constraint.
