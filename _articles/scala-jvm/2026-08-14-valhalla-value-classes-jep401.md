---
title: "Project Valhalla: Value Classes Land in Preview (JEP 401)"
date: 2026-08-14
track: scala-jvm
summary: "JEP 401 value classes are integrated as a preview feature in JDK 28. This note examines what 'codes like a class, works like an int' denotes, how == is redefined for value objects, and how the model relates to Scala's AnyVal value classes and opaque types."
reading_time: 6
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

**Gist.** Every non-primitive Java object carries an *identity* fixed at construction, which normally forces the Java Virtual Machine (JVM) to place it on the heap and reach it through a pointer — escape analysis removes that cost only where the compiler can prove the object never escapes — even for aggregates such as `Point` or `Complex` where identity is never consulted. **JEP 401, Value Classes and Objects**, integrated for JDK 28 as a **preview** feature, adds a `value` class modifier whose instances are distinguished solely by field state, permitting the JVM to scalarize or flatten them. The cost of discarding identity is that the operations identity supported disappear with it: value objects cannot be synchronized on, their fields are implicitly final, and `==` changes meaning.

## Identity as an obligation

An *identity object* is one whose distinctness is independent of its contents: two instances with byte-identical fields remain different objects. That distinctness is what makes reference equality and monitor-based locking meaningful, and it constrains the runtime. An identity object must have a stable address a reference can name, which implies an **object header** and an **indirection on every field access**, with the attendant cache miss when the referent is not resident.

A value class waives that guarantee. Declaring a class with the `value` modifier states that its instances are **equivalent whenever their fields are equivalent**, so the runtime may duplicate, discard or re-create an instance freely. Two representational optimizations follow. **Scalarization** decomposes an instance into its constituent fields held in registers or on the stack, eliminating the allocation entirely. **Flattening** embeds the fields inline in an enclosing object or array element, removing both the header and the pointer hop.

Waiving identity is not free. JEP 401 makes the fields of a value class **implicitly final**, and value objects **cannot be used as monitors** — the operations that depended on a unique address are withdrawn rather than emulated.

## The redefinition of `==`

The visible language change is the semantics of `==`. For identity classes the operator is unchanged and compares references. For a **value object**, `a == b` holds when both operands are instances of the same value class and their corresponding fields are equal, with reference-typed fields compared recursively by `==`. The operator therefore becomes **statewise equality** for value objects.

```java
// JDK 28, compiled and run with --enable-preview
value record Complex(double re, double im) {
    Complex plus(Complex o) { return new Complex(re + o.re, im + o.im); }
}

Complex a = new Complex(1, 2);
Complex b = new Complex(1, 2);
System.out.println(a == b);   // true -> statewise, no identity
```

The consequence for existing code is that `==` no longer answers a single question uniformly: its meaning now depends on whether the static type in question is a value class. Code that relied on `==` returning `false` for separately constructed instances — identity-keyed caches, instance-counting, sentinel objects compared by reference — changes behaviour if the class it operates on is converted to a value class.

The `value` modifier applies to plain classes and to records. **All fields must be assigned before an instance becomes observable**: JEP 401 builds on strict field semantics, under which the fields of a value class are assigned before the superclass constructor runs and bytecode verification enforces that ordering. A partially initialized value object would otherwise be observable, since a scalarized or flattened representation has no single reference publication to fence.

## What has and has not shipped

JEP 401 is a **preview** feature targeted at **JDK 28**, which reaches general availability in March 2027. Preview features are **disabled by default and require `--enable-preview` at both compile time and run time**; early-access builds are published at `jdk.java.net/valhalla`.

The distinction worth holding is between the *object model* and the *layout optimizations*. JEP 401 delivers the model — the `value` modifier, the loss of identity, the redefined `==` — and leaves the density work to follow-on proposals covering **null-restricted and nullable value types**, **enhanced primitive boxing**, and, later, primitive value classes and JVM specialization. Heap flattening in particular is largely absent from this preview, because a value type that admits `null` still needs a representation for the null case. Code compiled against JEP 401 today acquires the semantics without the guarantee of `int`-like density.

## Relation to Scala's existing constructs

Scala has approximated the same goal from the language side with two mechanisms, both of which are erased rather than represented natively by the JVM.

An `AnyVal` value class is compiled to its single underlying field where the compiler can prove the wrapper is unnecessary, and **boxes wherever the erased type is insufficient**: generic positions, arrays, and other contexts where an `Object` reference is required. An `opaque type` is a compile-time alias that carries **no runtime representation at all**; it provides type distinctness with zero overhead but is confined to a single underlying type and offers no multi-field aggregate.

Neither construct extends to a multi-field aggregate flattened by the runtime. JEP 401 places the notion of a value below both, in the JVM object model, where a multi-field aggregate can in principle be scalarized without the language compiler proving anything about it. Until the follow-on layout JEPs arrive, JEP 401 is best treated as the **semantic foundation** that Scala's abstractions have been simulating from above.

### Implementation sketch (Scala)

The boxing boundary of `AnyVal` is the load-bearing difference, and it is observable without any Valhalla build:

```scala
final case class UserId(value: Long) extends AnyVal:
  def next: UserId = UserId(value + 1L)

opaque type Meters = Double
object Meters:
  def apply(d: Double): Meters = d
  extension (m: Meters) def toDouble: Double = m

// Monomorphic call: the parameter is erased to a primitive long,
// so no UserId instance is materialised.
def bumpDirect(id: UserId): UserId = id.next

// Generic position: T erases to Object, so the argument must be
// boxed into a real UserId instance at the call site.
def firstOf[T](xs: Seq[T]): T = xs.head

// Array of a value class is an array of references, not of longs.
val ids: Array[UserId] = Array(UserId(1L), UserId(2L))

// Opaque types have no wrapper to box: Meters *is* Double after
// erasure, which also means Array[Meters] is Array[Double].
val distances: Array[Meters] = Array(Meters(1.5), Meters(2.5))
```

`bumpDirect` operates on an unboxed `long`; `firstOf(ids.toSeq)` returns a boxed `UserId`. The rule that produces both outcomes is erasure, not a runtime decision, which is why the guarantee cannot be extended to a two-field aggregate without support from the JVM.

## Pitfalls

- **Assuming performance parity with `int` today.** JEP 401 ships the object model; heap flattening and null-restricted layouts belong to later JEPs, so a `value record` benchmarked on a JDK 28 preview build may allocate exactly as its identity counterpart does.
- **Converting an identity class relied on for reference equality.** Adding `value` to a class silently changes `==` from reference to statewise comparison, so an identity-keyed cache or a sentinel instance compared with `==` starts matching unrelated equal-valued instances.
- **Synchronizing on a value object.** Value objects have no monitor; a `synchronized` block over one fails rather than degrading to a silent no-op, so lock-bearing types cannot be converted.
- **Mutating fields after conversion.** Fields of a value class are implicitly final, so any class with an assignable field fails to compile with the modifier applied.
- **Omitting `--enable-preview` at run time.** Preview features require the flag at both compile and run time; supplying it only at compilation yields a failure when the class is loaded, not when it is built.
- **Expecting Scala `AnyVal` to avoid boxing uniformly.** An `AnyVal` value class is erased to its field only in monomorphic positions; in generic contexts, arrays and other reference positions it boxes, so `Array[UserId]` stores references rather than `long` values.
