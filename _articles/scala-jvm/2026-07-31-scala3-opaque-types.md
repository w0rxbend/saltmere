---
title: "Opaque Types in Scala 3: Type Safety That Vanishes at Runtime"
date: 2026-07-31
track: scala-jvm
summary: "Scala 3 opaque type aliases produce distinct domain types such as UserId and Meters that the compiler enforces, then erase to the underlying primitive with no allocation and no boxing."
reading_time: 6
tags: [scala3, opaque-types, type-safety, zero-cost, domain-modeling, jvm]
sources:
  - title: "Opaque Types — Scala 3 Book"
    url: "https://docs.scala-lang.org/scala3/book/types-opaque-types.html"
  - title: "SIP-35: Opaque Type Aliases"
    url: "https://docs.scala-lang.org/sips/opaque-types.html"
  - title: "Opaque Type Aliases: More Details (Reference)"
    url: "https://docs.scala-lang.org/scala3/reference/other-new-features/opaques-details.html"
  - title: "Scala 3: Opaque Types Quickly Explained — Rock the JVM"
    url: "https://rockthejvm.com/articles/scala-3-opaque-types"
  - title: "Opaque Type Alias in Scala 3 — Baeldung on Scala"
    url: "https://www.baeldung.com/scala/opaque-type-alias"
---

**Gist.** A domain that represents user identifiers, order identifiers and account balances all as `Int` or `Long` gives the compiler nothing to check: the arguments of `transfer(fromId, toId)` are mutually substitutable, and so are an order number and a user number. Scala 3's **opaque type aliases** make the equivalence between the new type and its underlying representation visible only inside the scope that declares it, so the type checker rejects the mixing while the erased bytecode still carries the raw primitive. The cost is that every operation the new type is allowed to support must be written out by hand — extension methods and constructors — because outside the defining scope the type has no inherited surface at all.

## The declaration and its scope rule

An opaque type alias is declared inside an enclosing object, class or trait, which supplies the scope the rule below refers to:

```scala
object UserIds:
  opaque type UserId = Long
```

Inside `UserIds`, `UserId` and `Long` are interchangeable in both directions: a `Long` is accepted where a `UserId` is expected and the reverse. Outside, they are unrelated. The Scala documentation states the rule directly — the fact that `UserId` is the same as `Long` "is only known in the scope where it is defined." The alias is therefore **transparent inside and opaque outside**, and that asymmetry is the entire mechanism.

The consequence for callers is mechanical rather than stylistic. Outside the defining scope, `UserId` behaves as an **abstract type**: there is no implicit widening to `Long`, no inherited arithmetic, and no `.value` accessor unless one is exported deliberately. Two opaque aliases declared over the same representation — `UserId` and `OrderId`, both `Long` — are distinct abstract types to the checker, so assigning one to the other is a compile error even though the runtime values are indistinguishable.

## Bounds relax the boundary in one direction

The reference documentation defines the general form with an upper bound:

```scala
opaque type UserId <: Long = Long
```

Outside the defining scope the alias is then abstract **with that bound**, which means a `UserId` is usable where a `Long` is expected, while a `Long` is still rejected where a `UserId` is expected. This is the one-way relaxation: lifting stays controlled, unlifting becomes free. Omitting the bound seals both directions. The choice is load-bearing, because a bounded alias silently permits `id + 1` to typecheck as `Long` arithmetic and lose the domain type from the result.

## Constructors and extension methods define the whole API

Because nothing leaks across the boundary, the visible interface consists of exactly what the defining scope exposes. The conventional shape is a companion object holding a validating constructor plus extension methods supplying behaviour:

```scala
object Ids:
  opaque type UserId = Long
  opaque type OrderId = Long

  object UserId:
    // validation happens before the value is lifted into the opaque type
    def from(raw: Long): Option[UserId] =
      Option.when(raw > 0)(raw)

    def unsafe(raw: Long): UserId = raw

  extension (id: UserId)
    def value: Long = id
    def next: UserId = id + 1

// outside the defining scope:
import Ids.*

val maybeUser: Option[UserId] = UserId.from(42L)
val order: OrderId = ???

maybeUser.foreach { u =>
  println(u.value)   // 42, through the extension method
  // val bad: OrderId = u   // compile error: UserId is not OrderId
  // val n: Long = u        // compile error: UserId is not Long
}
```

The split between `from` and `unsafe` separates the checked entry point offered to callers from the unchecked lift used by code inside the trust boundary. Both compile to identity: the body `raw` typechecks only because `UserId` and `Long` coincide inside `Ids`. Extension methods are resolved statically at compile time, and since the alias is erased to `Long`, `value` is the identity on a `Long` and `next` is integer addition — no wrapper object participates in either.

## Cost relative to the two alternatives

Against **raw primitives**, the gain is the compile error shown above and signatures that name the domain concept rather than its representation. Against **wrapper classes**, the gain is the allocation avoided. A `case class UserId(value: Long)` allocates a heap object per identifier that the runtime must then keep or collect. An `AnyVal` value class, `class UserId(val value: Long) extends AnyVal`, avoids that allocation only in the cases where the compiler can keep the underlying value unwrapped. Wherever the value has to be treated as a reference — used as a type argument to a generic signature, stored in an array of the value-class type, or assigned to `Any` — the wrapper is instantiated after all, so the allocation reappears in the positions that are hardest to spot by reading a call site.

Opaque types have no equivalent cliff, because there is no second implementation to fall back to: the alias is erased to its representation. SIP-35 sets exactly this as the goal, that operations on such wrapper types should not add runtime overhead while still being type-safe at compile time; the Scala 3 documentation describes opaque types as providing the abstraction without runtime overhead.

Erasure covers the alias, not the code written around it. An opaque type backed by a reference type whose extension methods build intermediate objects still allocates those objects. The wrapper is free; the contents of the extension methods are not.

### Implementation sketch (Scala)

Units of measure exercise the same mechanism with a second failure the identifier case does not show — arithmetic that must not silently mix dimensions:

```scala
object Units:
  opaque type Meters  = Double
  opaque type Seconds = Double

  object Meters:
    def from(d: Double): Option[Meters] = Option.when(d >= 0.0)(d)

  object Seconds:
    def from(d: Double): Option[Seconds] = Option.when(d > 0.0)(d)

  extension (m: Meters)
    def toDouble: Double = m
    def +(other: Meters): Meters = m + other   // resolves inside the scope
    // no `+ (s: Seconds)` overload exists, so mixing units cannot compile

  extension (s: Seconds)
    def toDouble: Double = s

  // dimensional result stays a plain Double: no MetersPerSecond alias declared
  def speed(m: Meters, s: Seconds): Double = m.toDouble / s.toDouble

import Units.*

val d = Meters.from(120.0)
val t = Seconds.from(9.6)
for { m <- d; s <- t } yield speed(m, s)   // Some(12.5)
// val wrong = d.map(_ + t.get)        // compile error: Seconds is not Meters
```

The `+` extension is recursive-looking but is not: inside `Units`, `m` and `other` are `Double`, so the body selects primitive addition rather than the extension method.

## Pitfalls

- **A runtime type test cannot recover the opaque type.** After erasure only the representation survives, so any check performed at runtime sees a `Long` and cannot distinguish a `UserId` from an `OrderId` declared over the same representation.
- **An upper bound removes the seal in one direction for every caller.** Declaring `opaque type UserId <: Long = Long` makes every `Long`-accepting method applicable to a `UserId`, including arithmetic whose result is typed `Long` and has lost the domain type.
- **Exposing an unchecked lift alongside the validating one defeats the validation.** A public `unsafe` constructor is a hole through which unvalidated values enter the type; the invariant holds only if that entry point stays inside the trust boundary.
- **The defining scope is the trust boundary, so a large one weakens the type.** Every declaration sharing the enclosing object, class or trait sees `UserId` and `Long` as interchangeable; putting unrelated code in that scope hands it the unchecked lift for free.
- **Erasure applies to the alias, not to what the extension methods construct.** An opaque type over a reference type whose methods allocate intermediate structures allocates once per call, and the absence of wrapper allocation says nothing about that.
- **Two aliases over the same representation are interchangeable across a serialization boundary.** Distinctness is a compile-time property; a payload written as a `Long` carries no evidence of which alias produced it.
