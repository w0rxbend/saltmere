---
title: "Named Tuples: Scala 3's Labels Without a Class"
date: 2026-07-30
track: scala-jvm
summary: "Scala 3.7 stabilized named tuples — `(name = \"Ada\", age = 36)` typed as `(name: String, age: Int)`. Field access by name, pattern matching, zero-cost conversions, and their role in query APIs."
reading_time: 6
tags: [scala-3, named-tuples, tuples, structural-types, dto]
sources:
  - title: "Scala 3.7.0 released! — scala-lang.org"
    url: "https://www.scala-lang.org/news/3.7.0/"
  - title: "Named Tuples — Scala 3 Reference"
    url: "https://docs.scala-lang.org/scala3/reference/other-new-features/named-tuples.html"
  - title: "SIP-58: Named Tuples"
    url: "https://docs.scala-lang.org/sips/58.html"
  - title: "Scala stabilizes named tuples — InfoWorld"
    url: "https://www.infoworld.com/article/3984541/scala-stabilizes-named-tuples.html"
  - title: "Scala's New Named Tuples: why you should embrace structural types — bishabosha"
    url: "https://bishabosha.github.io/articles/named-tuples.html"
---

**Gist.** A positional tuple carries no information about what its slots mean: `result._1.name` names nothing, and two same-typed fields can be transposed without the compiler objecting. Scala 3.7.0, released **May 7, 2025**, stabilized **named tuples**, which attach a compile-time label to each element so that a value of type `(name: String, age: Int)` is selected by `.name` rather than `._1`. The cost is the cost of structural typing: the labelled type has **no nominal identity**, no methods, no companion, and no `copy` — two unrelated concepts that happen to share field names and types are the same type.

Named tuples first shipped as an experimental preview in Scala 3.5. This article is written against Scala 3.7+; the 3.3.x line remains the long-term-support (LTS) series, and it predates the feature entirely.

## The construct

An element is labelled with `name = value`:

```scala
val ada = (name = "Ada", age = 36)
```

The inferred type carries those labels, and the type itself can be aliased:

```scala
val ada: (name: String, age: Int) = (name = "Ada", age = 36)

type Person = (name: String, age: Int)
```

The labels exist **only at compile time**. A named tuple is a zero-cost wrapper over the underlying plain `(String, Int)`: there is no additional runtime object and no allocation beyond that of an ordinary tuple.

Selection is by label:

```scala
ada.name   // "Ada"
ada.age    // 36
```

which propagates through ordinary collection combinators without any positional bookkeeping:

```scala
val people: List[Person] = List(ada, (name = "Alan", age = 41))
val minors = people.filter(p => p.age < 18)
val names  = people.map(_.name)
```

## Structural, not nominal

The distinction from `case class Person(name: String, age: Int)` is **nominal versus structural** typing. A case class introduces a *new named type*: `Person` is not `Employee` even when both hold a `String` followed by an `Int`. A named tuple has no such identity — it is a shape. Two named tuples with the same field names in the same order and with the same element types **are the same type**, regardless of where they are written.

SIP-58 targets the case of data substantial enough to deserve field names but too local or transient to justify a class declaration: no declaration, no import to share it, no name to invent.

| | Named tuple | Case class | Positional tuple |
|---|---|---|---|
| Field access | `.name` | `.name` | `._1` |
| Type identity | structural | nominal | structural |
| Declaration needed | no | yes (`case class …`) | no |
| Distinct from same-shape peer | no | yes | no |
| Methods / companions | no | yes | no |
| Runtime cost | none (erases to tuple) | class instance | tuple |

The trade-off is the one structural typing always imposes: zero declaration overhead, but no place to attach methods, no `copy`, and no guarantee that two `(name, age)` values denote the same domain concept. A case class remains the appropriate choice for an entity referenced across a codebase.

## Conversions and subtyping

**Ordering is significant.** `(name: String, age: Int)` and `(age: Int, name: String)` are different and mutually incompatible types; a value of one is not accepted where the other is expected, even though the field sets coincide.

The relationship to plain tuples is directional. A regular tuple is a **subtype** of the correspondingly shaped named tuple, so an unnamed value flows into a named position:

```scala
val raw: (String, Int) = ("Grace", 45)
val g:   (name: String, age: Int) = raw   // OK — names added
```

In the reverse direction `.toTuple` discards the labels and yields the positional tuple, and the reference specifies that **the compiler inserts a `.toTuple` selection implicitly** when it meets a named tuple where a regular tuple is expected:

```scala
val bare: (String, Int) = ada.toTuple      // ("Ada", 36)
val also: (String, Int) = ada              // .toTuple inserted by the compiler
```

That insertion does not reach inside type constructors: a `List[(name: String, age: Int)]` is not accepted where a `List[(String, Int)]` is expected, and the elements must be mapped through `.toTuple` explicitly.

Case classes bridge in through `NamedTuple.From`, a type-level function computing the named-tuple shape of a case class's fields:

```scala
case class City(zip: Int, name: String, population: Int)
// NamedTuple.From[City]  ==  (zip: Int, name: String, population: Int)
```

## Pattern matching

Named tuples destructure positionally, as ordinary tuples do:

```scala
ada match
  case (n, a) => println(s"$n is $a")
```

They also match **by name**, in which case only the fields of interest need be mentioned and their order in the pattern is free:

```scala
ada match
  case (age = a) => println(a)              // partial, one field
  case (age = a, name = n) => println(n)    // reordered
```

The same named-field pattern syntax extends to case-class patterns, removing the row of `_` wildcards otherwise required to reach a single field:

```scala
city match
  case City(name = "London") => city.population
  case City(name = n, zip = 1026) => n
```

## Multiple return values

A function returning several values needs neither a throwaway class nor an opaque `(A, B)`:

```scala
def minMax(xs: List[Int]): (min: Int, max: Int) =
  (min = xs.min, max = xs.max)

val r = minMax(List(3, 9, 1, 7))
println(s"${r.min}..${r.max}")   // 1..9
```

In the positional formulation `xs => (xs.min, xs.max)`, the association of `._1` with the minimum exists only in the caller's memory. With the labelled result it is present in the type, and therefore in the signature and the diff.

## Schemas as types

Because a named tuple is a first-class *value* of a *structural* type, a library can inspect and transform its schema at the type level — work that previously required macros. A class extending `Selectable` may expose a `Fields` member typed as a named tuple, and the compiler resolves selections such as `.columnName` against it. Together with type-level helpers such as `NamedTuple.From` and `NamedTuple.Map`, this supports column APIs checked at compile time. The sketch below is the data-frame example from bishabosha's write-up, where `text` is a string of words and `toLower` a `String => String`:

```scala
val stats = DataFrame
  .column((words = text.split("\\s+")))
  .withComputed((lowerCase = fun(toLower)(col.words)))
  .groupBy(col.lowerCase)
  .agg(group.key ++ (freq = group.size))
```

`col.words` and `col.lowerCase` are checked against the frame's evolving schema, so a misspelled column is a compile error, and `++` concatenates named tuples to widen the result shape. The general pattern in emerging Scala data-frame and structured query language (SQL) libraries is that **the query result type is a named tuple**, so the set of selected columns and the set of accessible downstream fields are the same set by construction — without code generation and without string-keyed lookup.

### Implementation sketch (Scala)

The load-bearing mechanism behind such an API is `Selectable` with a `Fields` type derived from the schema. The runtime value is a positional row; the field names live only in `Fields`, and `selectDynamic` is the erased accessor the compiler emits every named selection into.

```scala
import NamedTuple.{AnyNamedTuple, From}

// A row whose accessible field names are dictated by the type parameter N.
final class Row[N <: AnyNamedTuple](
    private val names: IndexedSeq[String],
    private val cells: IndexedSeq[Any]
) extends Selectable:
  type Fields = N

  // Every `row.someLabel` selection compiles to this call.
  def selectDynamic(field: String): Any =
    cells(names.indexOf(field))

object Row:
  // Build a row from a case class, deriving its schema shape at the type level.
  def of[C <: Product](c: C): Row[From[C]] =
    Row(
      c.productElementNames.toIndexedSeq,
      c.productIterator.toIndexedSeq
    )

case class City(zip: Int, name: String, population: Int)

val r = Row.of(City(1026, "London", 8_866_000))
val n: String = r.name     // typed from Fields = (zip: Int, name: String, ...)
// r.nmae                  // compile error: no such field in Fields
```

The invariant is that `names` and `cells` are the same length and in the same order as the fields of `Fields`; nothing in the type system enforces it, so a constructor that permutes one without the other yields silently wrong values rather than a type error.

## Pitfalls

- **Field order is part of the type.** Passing a `(age: Int, name: String)` where `(name: String, age: Int)` is expected fails to compile despite identical field sets; the fix is to reorder the literal, not to reorder the parameter.
- **Subtyping is one-directional.** A plain `(String, Int)` is accepted where `(name: String, age: Int)` is expected, so an unlabelled value silently acquires labels that may be wrong; the reverse direction is not subtyping but an implicitly inserted `.toTuple`, which the compiler does not insert under a type constructor.
- **No nominal distinction.** Two named tuples describing unrelated concepts with coinciding field names and types are the same type, so a function expecting one accepts the other and the mistake surfaces only as wrong data.
- **No methods, no `copy`.** There is no companion object to hang smart constructors, validation or `copy` on; a type that needs any of these is a case class, and retrofitting one later changes every call site from structural to nominal.
- **`.toTuple` is lossy.** After discarding labels the result is an ordinary tuple accessed by `._1`, and re-widening it back to a named tuple is unchecked beyond arity and element types.
- **`selectDynamic` erases.** In a `Selectable`-based API the accessor returns an erased value cast by the compiler according to `Fields`; a runtime row whose column order disagrees with the declared schema produces a `ClassCastException` or wrong data at the use site rather than at construction.
