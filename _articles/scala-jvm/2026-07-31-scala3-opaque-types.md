---
title: "Opaque Types in Scala 3: Type Safety That Vanishes at Runtime"
date: 2026-07-31
track: scala-jvm
summary: "Scala 3 opaque type aliases give you distinct domain types like UserId and Meters that the compiler enforces, then erases to the underlying primitive with zero allocation and zero boxing."
reading_time: 5
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

Passing a bare `Int` around your domain is a bug waiting to happen. Every `UserId`, `OrderId`, and `accountBalance` is the same `Int` to the compiler, so nothing stops you from swapping the arguments to `transfer(fromId, toId)` or handing an order number to a function that wants a user. The usual fixes each cost something: raw primitives cost safety, and wrapper classes cost runtime allocation. Scala 3's **opaque type aliases** give you both — distinct types the compiler enforces, that erase to the underlying primitive at runtime.

## The syntax

You declare an opaque type inside an object or module:

```scala
object UserIds:
  opaque type UserId = Long
```

Inside `UserIds`, `UserId` and `Long` are interchangeable. Everywhere else, `UserId` is a completely distinct, incompatible type. As the Scala documentation puts it: the fact that `UserId` is the same as `Long` "is only known in the scope where it is defined." That one-way transparency is the whole trick — the alias is *transparent inside* and *opaque outside*.

## Why the scope boundary matters

Because the equivalence is invisible outside the defining object, callers cannot accidentally treat a `UserId` as a `Long` (or as an `OrderId`). There is no implicit widening, no `.value` escape hatch you didn't write yourself. You decide exactly how values are *lifted* into the type and *unlifted* back out, by choosing which constructors and extension methods to expose. Everything else stays sealed.

## Smart constructors and extension methods

Since the type is opaque from the outside, you must provide the API deliberately. A companion gives you a smart constructor for validation, and extension methods give the type behavior without inheriting the underlying type's full (and often unsafe) surface:

```scala
object Ids:
  opaque type UserId = Long
  opaque type OrderId = Long

  object UserId:
    // smart constructor: validate before lifting
    def from(raw: Long): Option[UserId] =
      Option.when(raw > 0)(raw)

    // trusted, unchecked lift for internal use
    def unsafe(raw: Long): UserId = raw

  extension (id: UserId)
    def value: Long = id
    def next: UserId = id + 1

// usage, outside the defining scope:
import Ids.*

val maybeUser: Option[UserId] = UserId.from(42L)
val order: OrderId = ???

maybeUser.foreach { u =>
  println(u.value)   // 42, via the extension method
  // val bad: OrderId = u   // compile error: UserId is not OrderId
  // val n: Long = u        // compile error: UserId is not Long
}
```

The `from`/`unsafe` split is idiomatic: expose a validating constructor to the world, keep a raw lift for code you trust. Extension methods like `value` and `next` are resolved at compile time and, for a `Long`, compile down to plain field access and integer arithmetic.

## Why they beat raw primitives *and* wrapper classes

Against **raw primitives**, opaque types win on safety: `UserId` and `OrderId` are both `Long` underneath, yet mixing them is a compile error. You get self-documenting signatures for free.

Against **wrapper classes**, they win on cost. A `case class UserId(value: Long)` allocates a heap object per id. An `AnyVal` value class (`class UserId(val value: Long) extends AnyVal`) *tries* to avoid that, but the SIP is blunt about its failure modes: boxing happens "anywhere in the program where the type signatures are generic and require the runtime to pass a `java.lang.Object`" — arrays, generic collections, pattern matches, `equals`/`hashCode`, and functions all reintroduce allocation. Opaque types have no such cliff. Because there is only one implementation and it is fully erased, the Scala documentation states there is "no boxing overhead for primitive types" — a `UserId` *is* a `Long` in the bytecode. The stated design goal of SIP-35 is exactly this: "operations on these wrapper types must not create any extra overhead at runtime while still providing a type safe use at compile time."

The one caveat worth knowing: erasure is guaranteed for the opaque type itself, but if you back an opaque type with a reference type and add methods that construct intermediate objects, those objects still exist. The *wrapper* is free; whatever you build inside your extension methods is not.

## When to reach for them

Use opaque types for identifiers, units of measure (`Meters`, `Seconds`, `Celsius`), validated strings (`Email`, `NonEmptyString`), and any place a primitive is standing in for a domain concept. They are the default choice in Scala 3 for the "make illegal states unrepresentable" habit, precisely because the safety is free.

**Try next:** Define `opaque type Meters = Double` and `opaque type Seconds = Double` in one object, give each a smart constructor, then write an extension method `def speed(m: Meters, s: Seconds): Double`. Compile with `-Xprint:erasure` (or `scalac -Vprint:erasure`) and confirm both types have collapsed to `double` in the erased tree — no boxes, no wrappers.
