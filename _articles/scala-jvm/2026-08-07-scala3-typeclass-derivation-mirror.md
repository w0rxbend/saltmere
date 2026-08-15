---
title: "Typeclass Derivation in Scala 3: the derives Clause and Mirror"
date: 2026-08-07
track: scala-jvm
summary: "Scala 3 synthesises a compiler-native Mirror for every case class, enum, and sealed hierarchy. An inline derived method plus scala.compiletime turns that Mirror into given instances at the use site, without Shapeless and without a macro library. This article describes how MirroredElemTypes, MirroredElemLabels, ProductOf, and SumOf fit together, using a Show[A] derivation."
reading_time: 7
tags: [scala3, typeclasses, derivation, mirror, metaprogramming]
sources:
  - title: "Type Class Derivation — Scala 3 Reference"
    url: "https://docs.scala-lang.org/scala3/reference/contextual/derivation.html"
  - title: "scala.deriving.Mirror — Scala 3 API"
    url: "https://www.scala-lang.org/api/current/scala/deriving/Mirror$.html"
  - title: "Mirror.scala (standard library source)"
    url: "https://github.com/scala/scala3/blob/main/library/src/scala/deriving/Mirror.scala"
  - title: "scala.compiletime — Scala 3 API"
    url: "https://www.scala-lang.org/api/current/scala/compiletime.html"
  - title: "Mastering Typeclass Derivation with Scala 3 — Lunatech"
    url: "https://blog.lunatech.com/posts/2025-03-07-typeclass-derivation"
---

**Gist.** Writing a `Show`, an `Eq`, or a JSON `Encoder` by hand for every case class is boilerplate that Scala 2 offloaded to Shapeless, a macro-heavy library that reflected types into `HList`s and `Coproduct`s. Scala 3 moves that machinery into the compiler: every case class, enum, and sealed hierarchy carries a synthesised `scala.deriving.Mirror` exposing its shape at the type level, and a `derives` clause attaches the typeclass instance at the definition site. The cost is paid at compile time — the derivation is an `inline` expansion that unrolls once per derived type, and its scope is limited to shape and labels rather than a full generic-programming toolkit.

## The synthesised Mirror

For any `T` that is a case class, singleton, enum, or sealed trait, the compiler can materialise a `Mirror.Of[T]`. The value itself carries no data; its content is entirely in its type members:

```scala
sealed trait Mirror:
  type MirroredMonoType          // the type with its type arguments replaced by wildcards
  type MirroredType              // the type, fully applied
  type MirroredLabel <: String   // the type's name, as a singleton string
```

The two members a derivation reads most — `MirroredElemTypes` and `MirroredElemLabels`, both `Tuple`s — are not declared on that trait. `Mirror.Of[T]`, `Mirror.ProductOf[T]` and `Mirror.SumOf[T]` are refinement aliases that pin `MirroredType` and `MirroredMonoType` to `T` and require `MirroredElemTypes <: Tuple`; the mirror value the compiler synthesises defines the element types and the element labels concretely.

Two subtraits specialise the base. **`Mirror.Product`, refined as `Mirror.ProductOf[T]`, describes a product** — a case class or a single enum case — and adds `fromProduct(p: Product): MirroredMonoType`, a constructor taking an untyped `Product` and rebuilding a `T`. **`Mirror.Sum`, refined as `Mirror.SumOf[T]`, describes a sum** — an enum or sealed trait — and adds `ordinal(x: MirroredMonoType): Int`, the index of the case a value belongs to. `Mirror.Of[T]` is the umbrella alias summoned when the shape is not known in advance.

The load-bearing member is **`MirroredElemTypes`**, a `Tuple` of the constituent types. For a product these are the field types in declaration order; for a sum they are the types of its cases, in declaration order:

```scala
case class Point(x: Int, y: Int)
// Mirror.Of[Point]:
//   MirroredMonoType   = Point
//   MirroredLabel      = "Point"
//   MirroredElemTypes  = (Int, Int)
//   MirroredElemLabels = ("x", "y")

enum Shape:
  case Circle(radius: Int)
  case Rectangle(width: Int, height: Int)
// Mirror.Of[Shape]:
//   MirroredLabel      = "Shape"
//   MirroredElemTypes  = (Shape.Circle, Shape.Rectangle)
//   MirroredElemLabels = ("Circle", "Rectangle")
```

The asymmetry is the invariant a derivation must respect: **for a product `MirroredElemTypes` are fields, for a sum they are cases**. Derivation code branches on `ProductOf` versus `SumOf` because the two members mean different things.

## From derives to given

The `derives` clause is sugar. `case class Point(x: Int, y: Int) derives Show` instructs the compiler to synthesise a given in `Point`'s companion, equivalent to:

```scala
given Show[Point] = Show.derived[Point]  // using the summoned Mirror.Of[Point]
```

**The contract a typeclass must satisfy is that its companion object defines a member named `derived` whose result is an instance for the derived type.** Mirror-based derivations declare it as an `inline def` taking a `using Mirror.Of[T]`, but the compiler checks only that the call `Show.derived[Point]` typechecks, so other shapes of `derived` are admissible. Because this `derived` is `inline`, it expands — Mirror and all — where the synthesised given is defined, into straight-line code. No runtime reflection is involved; the generic part exists only during compilation.

### Implementation sketch (Scala)

A `Show[A]` that renders products with their field labels and dispatches sums on ordinal:

```scala
import scala.deriving.Mirror
import scala.compiletime.{constValue, constValueTuple, erasedValue, error, summonInline}

trait Show[A]:
  def show(a: A): String

object Show:
  given Show[Int]    = _.toString
  given Show[String] = s => s"\"$s\""

  // Walk the element-type tuple, producing one Show per element, in order.
  inline def summonAll[T, Elems <: Tuple]: List[Show[?]] =
    inline erasedValue[Elems] match
      case _: EmptyTuple      => Nil
      case _: (elem *: elems) => deriveOrSummon[T, elem] :: summonAll[T, elems]

  // An element that is itself a case of the sum has no user-written instance,
  // so derive it recursively; anything else must have a given in scope.
  inline def deriveOrSummon[T, Elem]: Show[Elem] =
    inline erasedValue[Elem] match
      case _: T => deriveChild[T, Elem]
      case _    => summonInline[Show[Elem]]

  inline def deriveChild[T, Elem]: Show[Elem] =
    inline erasedValue[T] match
      case _: Elem => error("infinite recursive derivation")
      case _       => derived[Elem](using summonInline[Mirror.Of[Elem]])

  // Lower the compile-time tuple of label singletons to runtime strings.
  inline def labelsOf[Elems <: Tuple]: List[String] =
    constValueTuple[Elems].productIterator.map(_.toString).toList

  private def showProduct[A](
      name: String, labels: List[String], instances: List[Show[?]]): Show[A] =
    (a: A) =>
      if labels.isEmpty then name  // a bare singleton case, e.g. an enum value
      else
        a.asInstanceOf[Product].productIterator
          .zip(labels).zip(instances)
          .map { case ((v, label), inst) =>
            s"$label = ${inst.asInstanceOf[Show[Any]].show(v)}" }
          .mkString(s"$name(", ", ", ")")

  private def showSum[A](s: Mirror.SumOf[A], instances: List[Show[?]]): Show[A] =
    (a: A) => instances(s.ordinal(a)).asInstanceOf[Show[Any]].show(a)

  inline def derived[A](using m: Mirror.Of[A]): Show[A] =
    lazy val instances = summonAll[A, m.MirroredElemTypes]
    inline m match
      case p: Mirror.ProductOf[A] =>
        showProduct(constValue[p.MirroredLabel], labelsOf[p.MirroredElemLabels], instances)
      case s: Mirror.SumOf[A] =>
        showSum(s, instances)
```

The use site:

```scala
case class Point(x: Int, y: Int) derives Show

enum Shape derives Show:
  case Circle(radius: Int)
  case Rectangle(width: Int, height: Int)

@main def demo(): Unit =
  println(summon[Show[Point]].show(Point(1, 2)))
  //=> Point(x = 1, y = 2)
  println(summon[Show[Shape]].show(Shape.Rectangle(3, 4)))
  //=> Rectangle(width = 3, height = 4)
```

## Reading the mechanism

Four `scala.compiletime` primitives carry the derivation.

**`erasedValue[T]` produces a phantom value of type `T` that never executes.** It exists so an `inline match` can pattern-match on its *type*. That is how `summonAll` peels a tuple: `case _: (elem *: elems)` binds `elem` to the head type and `elems` to the tail, recursing until `EmptyTuple`. The match is resolved during compilation, so what reads as a list traversal unrolls into a fixed sequence of `summonInline` calls with no loop at runtime.

**`summonInline[Show[Elem]]` is `summon` deferred to the expansion site.** Because `derived` is inline, `Elem` is concrete once the call is spliced in, so the compiler can locate `Show[Int]`, `Show[String]`, and the rest. A plain `using` parameter would force the search at the definition of `derived`, where the element types are still abstract; `summonInline` is what moves the search to a point where they are known.

**`constValue[p.MirroredLabel]` converts a singleton-string type (`"Point"`) into the value `"Point"`.** `constValueTuple[p.MirroredElemLabels]` does the same across a tuple, yielding `("x", "y")` — field names recovered without reflection.

The recursive `deriveOrSummon`/`deriveChild` pair follows the pattern given in the Scala 3 reference. A sum's `MirroredElemTypes` are its cases, and those cases have no user-written `Show`, so when an element type is a subtype of the sum (`case _: T`) the instance is derived inline rather than summoned. **The `deriveChild` guard rejects the degenerate `T <: Elem` case with a compile-time `error`, terminating what would otherwise be unbounded inline recursion.** For ordinary fields — an `Int` in `Point` — the element is not a subtype of `T`, so the other branch summons the given.

## Contrast with Shapeless

The Scala 2 stack this replaces: Shapeless converted a case class to a heterogeneous list (`HList`) via a macro-materialised `Generic`; `LabelledGeneric` tagged each element with its field name using `Witness`-encoded singleton types; sums became `Coproduct`s. Instances were assembled by implicit resolution recursing over the `::` structure. The costs were a third-party library on the classpath, macro expansion added to compile time, and error messages surfacing inside `HList` machinery.

Scala 3 maps each piece onto a compiler primitive: `Tuple` replaces `HList`, `MirroredElemLabels` replaces the `Witness`-tagged labels, and `ordinal`/`fromProduct` replace the `Coproduct`/`Generic` plumbing, with `derives` attaching the given at the definition site. Nothing is added to the build. **The trade-off is scope: `Mirror` exposes shape and labels only** — not polymorphic functions or record operations. shapeless-3 still provides those, and builds its `K0`/`K1` derivation on top of these same Mirrors.

The model to retain: `derives` is a companion-method contract, `Mirror` is compile-time shape, `MirroredElemTypes` is a tuple to recurse over, and `inline` plus `scala.compiletime` is what removes the recursion before runtime.

## Pitfalls

- **Treating `MirroredElemTypes` uniformly.** For a sum the tuple holds case subtypes, not field types; code that assumes fields will summon instances for `Shape.Circle` and `Shape.Rectangle` and fail to find any user-written given.
- **Using a `using` parameter where `summonInline` is required.** Resolution then happens at the definition of `derived`, where element types are abstract, and the compiler reports a missing implicit for a type variable rather than for the concrete field type.
- **Omitting the `deriveChild` guard.** A type whose element set includes itself expands inline without a termination condition; the symptom is an inline-expansion limit error rather than a diagnostic naming the type.
- **Assuming derivation reaches nested types automatically.** A field whose type is another case class requires either its own `derives` clause or a given in scope; without one, `summonInline` fails at the expansion site with an error pointing at the outer derivation.
- **Relying on the `asInstanceOf[Show[Any]]` cast to keep instances aligned with fields.** The instance list is erased to `List[Show[?]]`, so pairing it with the wrong iterator order is a `ClassCastException` at render time, not a compile error.
- **Inline expansion per derived type.** Each `derives` clause expands the whole derivation body at that site, so compile time grows with the number of derived types, not with a single shared generic implementation.
