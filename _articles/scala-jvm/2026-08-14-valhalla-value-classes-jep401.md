---
title: "Project Valhalla: Value Classes Land in Preview (JEP 401)"
date: 2026-08-14
track: scala-jvm
summary: "After a decade of design, Project Valhalla's first language feature is real: JEP 401 value classes are integrated for preview in JDK 28. Here's what 'codes like a class, works like an int' actually means, why == changes for value objects, and how it maps onto Scala's value and opaque types."
reading_time: 5
tags: [valhalla, jep-401, jvm, value-classes, scala, performance]
sources:
  - title: "JEP 401: Value Classes and Objects (Preview) — OpenJDK"
    url: "https://openjdk.org/jeps/401"
  - title: "Project Valhalla — OpenJDK"
    url: "https://openjdk.org/projects/valhalla/"
  - title: "State of Valhalla, Part 1: The Road to Valhalla (Brian Goetz)"
    url: "https://openjdk.org/projects/valhalla/design-notes/state-of-valhalla/01-background"
  - title: "Try Out JEP 401 Value Classes and Objects — Inside.java"
    url: "https://inside.java/2025/10/27/try-jep-401-value-classes/"
  - title: "Project Valhalla's First Preview: JEP 401 Redefines == for Java Objects — InfoQ"
    url: "https://www.infoq.com/news/2026/08/jep401-value-objects-preview/"
---

Project Valhalla has been the JVM's longest-running "any year now" for so long that it became a running joke. That changes with **JEP 401, Value Classes and Objects**, which was integrated into the JDK 28 mainline in July 2026 as a **preview** feature. It is the first piece of Valhalla to reach developers' hands as an actual language construct — and it is deliberately modest, so it's worth being precise about what has and hasn't shipped.

## The goal: "codes like a class, works like an int"

Valhalla's slogan, from Brian Goetz's *State of Valhalla* notes, is that you should be able to write an abstraction that **codes like a class but works like an int**. Today every non-primitive in Java is an *identity object*: it has a unique identity assigned at construction, which forces the JVM to allocate it on the heap and dereference a pointer on every access. That identity buys you `==`-by-reference and locking, but for a `Point`, a `Complex`, or a `LocalDate`, you never wanted it — it's pure overhead: an object header, a pointer indirection, cache misses.

**Value classes** let you opt out of identity. Declare a class with the `value` modifier and its instances are distinguished *solely by their field values*. Fields become implicitly final; instances have no identity, cannot be synchronized on, and the JVM is free to **scalarize** them (break them into their fields in registers) or **flatten** them inline into arrays and enclosing objects — removing the header and the indirection.

## What actually changes: `==`

The most visible language change is that `==` is redefined for value objects. For an identity class it's unchanged (reference equality). For a **value object**, `a == b` succeeds when both are the same value class with equal fields, comparing reference-typed fields recursively with `==`. In other words, `==` becomes *statewise* equality — which is exactly what you always wanted for a value.

```java
// JDK 28, compiled/run with --enable-preview
value record Complex(double re, double im) {
    Complex plus(Complex o) { return new Complex(re + o.re, im + o.im); }
}

Complex a = new Complex(1, 2);
Complex b = new Complex(1, 2);
System.out.println(a == b);   // true  -> statewise, no identity
```

The `value` modifier works on plain classes and on records. Fields must all be assigned before the instance is observable; JEP 539 adds the bytecode verification for that stricter construction.

## Be precise about status

As of August 2026, JEP 401 is a **preview** feature integrated for **JDK 28** (which reaches GA in March 2027). It is disabled by default and requires `--enable-preview` at both compile and run time; you can also grab early-access builds from `jdk.java.net/valhalla`. Crucially, the *performance* payoff — heap flattening, null-restricted layouts — is largely **not** in this preview. JEP 401 delivers the object *model* and the `==` semantics; the layout optimizations arrive with follow-on JEPs for **null-restricted and nullable value types**, **enhanced primitive boxing**, and eventually primitive value classes and JVM specialization. Don't expect `int`-density from value classes yet.

## The Scala connection

Scala developers have been reaching for this idea for years, with two workarounds:

```scala
// Scala 3 value class: one field, boxing avoided in many (not all) cases
final case class UserId(value: Long) extends AnyVal

// Scala 3 opaque type: zero-overhead wrapper, erased to Long at runtime
opaque type Meters = Double
object Meters:
  def apply(d: Double): Meters = d
  extension (m: Meters) def toDouble: Double = m
```

`AnyVal` value classes and `opaque type` both try to give you a distinct type without paying for a wrapper object — but they're limited: `AnyVal` classes still box in generic contexts, arrays, and pattern matches, and opaque types are erased single-field aliases with no multi-field flattening. Valhalla pushes this guarantee down into the JVM itself, for arbitrary multi-field values, in a way both Scala features could eventually target. For now, treat JEP 401 as the *semantic* foundation Scala's abstractions have been simulating from above.

**Try next:** download a JDK 28 Valhalla early-access build, compile a `value record` with `javap -v` and compare its constant pool and flags to the identity version — then check whether `==` on two equal instances returns `true` and reason about where the JVM could scalarize it.
