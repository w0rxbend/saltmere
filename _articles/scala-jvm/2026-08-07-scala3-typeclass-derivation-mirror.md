---
title: "Typeclass Derivation in Scala 3: the derives Clause and Mirror"
date: 2026-08-07
track: scala-jvm
summary: "Scala 3 generates a compiler-native Mirror for every case class, enum, and sealed hierarchy. A single inline derived method plus scala.compiletime turns that Mirror into given instances at the use site — no Shapeless, no macro library. Here is how MirroredElemTypes, MirroredElemLabels, ProductOf, and SumOf fit together, with a working Show[A]."
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

Writing a `Show`, an `Eq`, or a JSON `Encoder` by hand for every case class is the kind of boilerplate that Scala 2 offloaded to Shapeless — a macro-heavy library that reflected your types into `HList`s and `Coproduct`s. Scala 3 pulls that machinery into the compiler itself. Every case class, enum, and sealed hierarchy comes with a compiler-synthesised `scala.deriving.Mirror` that exposes its shape at the type level, and a one-line `derives` clause wires your typeclass to it. No external library, no macro, stable since 3.0 and refined across the 3.x line (this compiles on 3.8.4 and the 3.3.8 LTS).

## What the compiler hands you

For any `T` that is a case class, singleton, enum, or sealed trait, the compiler can materialise a `Mirror.Of[T]`. It is a phantom-ish value whose interest lies entirely in its type members:

```scala
sealed trait Mirror:
  type MirroredMonoType          // the type, with type params erased to their bounds
  type MirroredType              // the type, fully applied
  type MirroredLabel <: String   // the type's name, as a singleton string
  type MirroredElemLabels <: Tuple  // field/case names, as a tuple of string singletons
```

Two refinements specialise it. `Mirror.ProductOf[T]` describes a product (a case class or a single enum case) and adds `fromProduct(p: Product): MirroredMonoType` — a constructor that takes an untyped `Product` and rebuilds a `T`. `Mirror.SumOf[T]` describes a sum (an enum or sealed trait) and adds `ordinal(x: MirroredMonoType): Int` — the index of the case a value belongs to. The umbrella alias `Mirror.Of[T]` is the union you summon when you do not yet know which one you will get.

The load-bearing member is `MirroredElemTypes`, a `Tuple` of the constituent types. For a product it is the field types in order; for a sum it is the subtype of each case:

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

Note the asymmetry: for a product `MirroredElemTypes` are *fields*, for a sum they are *cases*. Your derivation code branches on `ProductOf` versus `SumOf` precisely because the two mean different things.

## From derives to given

The `derives` clause is sugar. Writing `case class Point(x: Int, y: Int) derives Show` instructs the compiler to synthesise a given in `Point`'s companion:

```scala
given Show[Point] = Show.derived[Point]  // using the summoned Mirror.Of[Point]
```

The only contract your typeclass must satisfy is that its companion object defines a `derived` method taking a `using Mirror.Of[T]`. When someone later writes `summon[Show[Point]]`, resolution finds that synthesised given, which calls `derived`, which is `inline` and therefore expands — Mirror and all — at the call site into straight-line code. There is no runtime reflection; the "generic" part is entirely a compile-time expansion.

## A real typeclass, derived

Here is a complete `Show[A]` that renders products with their field labels and dispatches sums on ordinal. It compiles as written.

```scala
import scala.deriving.Mirror
import scala.compiletime.{constValue, constValueTuple, erasedValue, error, summonInline}

trait Show[A]:
  def show(a: A): String

object Show:
  // Base instances for the leaves.
  given Show[Int]     = _.toString
  given Show[String]  = s => s"\"$s\""
  given Show[Boolean] = _.toString

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

And the use site — the whole point:

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

Four `scala.compiletime` primitives do the work, and each is worth pinning down.

`erasedValue[T]` conjures a phantom value of type `T` that never runs — it exists only so an `inline match` can pattern-match on its *type*. That is how `summonAll` peels a tuple: `case _: (elem *: elems)` binds `elem` to the head type and `elems` to the tail, recursing until `EmptyTuple`. The match is resolved at compile time, so what looks like a list traversal unrolls into a fixed sequence of `summonInline` calls with no loop left at runtime.

`summonInline[Show[Elem]]` is `summon`, but deferred to the expansion site. Because `derived` is inline, `Elem` is concrete by the time the call is spliced in, so the compiler can find `Show[Int]`, `Show[String]`, and so on. Using `summonInline` rather than a plain `using` parameter is what lets the search happen per-element after inlining.

`constValue[p.MirroredLabel]` turns a singleton-string *type* (`"Point"`) into its *value* `"Point"`. `constValueTuple[p.MirroredElemLabels]` does the same across a whole tuple, giving `("x", "y")` — the runtime field names, recovered without reflection.

The recursive `deriveOrSummon`/`deriveChild` dance is the subtle part, and it is lifted almost verbatim from the reference. A sum's `MirroredElemTypes` are its cases, and those cases have no user-written `Show`. So when an element type is a subtype of the sum (`case _: T`), we derive it inline instead of summoning; the `deriveChild` guard rejects the degenerate `T <: Elem` case with a compile-time `error` to stop infinite recursion. For ordinary fields (an `Int` in `Point`) the element is not a subtype of `T`, so the other branch summons the given as usual.

## Contrast with Shapeless

If you carried a Scala 2 codebase, this replaces a familiar stack. Shapeless converted a case class to a heterogeneous `HList` via a macro-materialised `Generic`, and `LabelledGeneric` tagged each element with its field name using `Witness`-encoded singleton types; sums became `Coproduct`s. Instances were then built by implicit resolution recursing over the `::` structure. It worked, but the cost was real: a third-party library on the classpath, macro expansions that inflated compile times, and error messages that surfaced deep inside `HList` machinery.

Scala 3 collapses that. `Mirror` is compiler-native, so `Tuple` stands in for `HList`, `MirroredElemLabels` for the `Witness`-tagged labels, and `ordinal`/`fromProduct` for the `Coproduct`/`Generic` plumbing — with `derives` attaching the given at the definition site. There is nothing to add to your build. The trade-off is scope: `Mirror` gives you shape and labels, not the full generic-programming toolkit (polymorphic functions, record operations). When you genuinely need that, shapeless-3 still exists and builds its `K0`/`K1` derivation *on top of* these same Mirrors — the compiler primitive underneath is the one described here.

The mental model to keep: `derives` is a companion-method contract, `Mirror` is compile-time shape, `MirroredElemTypes` is a tuple you recurse over, and `inline` + `scala.compiletime` is what makes the recursion vanish before runtime.

**Try next:** Swap `Show` for a `JsonEncoder[A]` that emits `{"x":1,"y":2}` for products and a tagged object for sums, reusing `labelsOf` for the keys — then add a `Mirror.Product`'s `fromProduct` to write the matching `JsonDecoder[A]` and watch the two derivations mirror each other.
