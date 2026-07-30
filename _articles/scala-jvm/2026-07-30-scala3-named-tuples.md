---
title: "Named Tuples: Scala 3's Labels Without a Class"
date: 2026-07-30
track: scala-jvm
summary: "Scala 3.7 stabilized named tuples — `(name = \"Ada\", age = 36)` typed as `(name: String, age: Int)`. Field access by name, pattern matching, zero-cost conversions, and why they make query APIs ergonomic."
reading_time: 5
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

Positional tuples have always been Scala's quickest way to return two things at once, and they've always been slightly miserable to use on the receiving end. `result._1.name` tells you nothing; swap two fields of the same type and the compiler waves it through. Scala 3.7.0, released **May 7, 2025**, fixed this by promoting **named tuples** from an experimental feature (they first shipped as a preview in Scala 3.5) to a **stable** part of the language. This article is written against Scala 3.7+; by mid-2026 the release line has moved on to Scala 3.8.x, with 3.3.x as the current LTS.

## What a named tuple is

You attach a label to each element with `name = value`:

```scala
val ada = (name = "Ada", age = 36)
```

The inferred type carries those labels:

```scala
val ada: (name: String, age: Int) = (name = "Ada", age = 36)

type Person = (name: String, age: Int)   // a reusable alias
```

That's the whole idea: a tuple where each slot has a name. The names live **only at compile time** — a named tuple is a zero-cost wrapper around the plain `(String, Int)` underneath it, so there's no runtime object and no allocation you wouldn't already pay for an ordinary tuple.

## Field access by name

The payoff is that you read fields by label instead of by position:

```scala
ada.name   // "Ada"
ada.age    // 36
```

No `._1`, no counting commas. In a pipeline the intent survives:

```scala
val people: List[Person] = List(ada, (name = "Alan", age = 41))
val minors = people.filter(p => p.age < 18)
val names  = people.map(_.name)
```

## Not a case class

The obvious question: why not just write a `case class Person(name: String, age: Int)`? The distinction is **nominal vs. structural**. A case class defines a *new named type* — `Person` is not `Employee` even if both hold a `String` and an `Int`. A named tuple has no nominal identity; it's just a **structural** shape. Two named tuples with the same field names and types *are the same type*, wherever they're written.

That makes named tuples ideal for the case Martin Odersky's SIP-58 targets: data that's real enough to deserve field names but too local or too transient to justify a class declaration. No `case class` boilerplate, no import to share the definition, no name to invent.

| | Named tuple | Case class | Positional tuple |
|---|---|---|---|
| Field access | `.name` | `.name` | `._1` |
| Type identity | structural | nominal | structural |
| Declaration needed | no | yes (`case class …`) | no |
| Distinct from same-shape peer | no | yes | no |
| Methods / companions | no | yes | no |
| Runtime cost | none (erases to tuple) | class instance | tuple |

The trade-off is the same one structural typing always makes: named tuples give you zero-boilerplate ergonomics but no place to hang methods, no `copy`, no nominal guarantee that two `(name, age)` values mean the same *concept*. Reach for a case class when the type is a domain entity used across your codebase; reach for a named tuple when it's a lightweight, local shape.

## Conversions: names on, names off

Ordering is significant — `(name: String, age: Int)` and `(age: Int, name: String)` are **different, incompatible types**. But the relationship to plain tuples is well-defined. A regular tuple is a **subtype** of the matching named tuple, so you can drop an unnamed tuple into a named slot:

```scala
val raw: (String, Int) = ("Grace", 45)
val g:   (name: String, age: Int) = raw   // OK — names added
```

Going the other way, `.toTuple` forgets the labels and hands you the positional tuple back:

```scala
val bare: (String, Int) = ada.toTuple      // ("Ada", 36)
```

Case classes bridge in through `NamedTuple.From`, which computes the named-tuple shape of a case class's fields:

```scala
case class City(zip: Int, name: String, population: Int)
// NamedTuple.From[City]  ==  (zip: Int, name: String, population: Int)
```

## Pattern matching

Named tuples destructure positionally, exactly like ordinary tuples:

```scala
ada match
  case (n, a) => println(s"$n is $a")
```

But you can also match **by name**, mention only the fields you care about, and list them in any order:

```scala
ada match
  case (age = a) => println(a)              // partial, just one field
  case (age = a, name = n) => println(n)    // reordered
```

The same named-field syntax extends to case-class patterns, so you can match one field and ignore the rest without a row of `_` wildcards:

```scala
city match
  case City(name = "London") => city.population
  case City(name = n, zip = 1026) => n
```

## Returning multiple named values

The everyday win is functions that return more than one thing without either a throwaway class or an opaque `(A, B)`:

```scala
def minMax(xs: List[Int]): (min: Int, max: Int) =
  (min = xs.min, max = xs.max)

val r = minMax(List(3, 9, 1, 7))
println(s"${r.min}..${r.max}")   // 1..9
```

Compare the positional version, `xs => (xs.min, xs.max)`, whose caller has to *remember* that `._1` is the min. With the named result the meaning is on the value, in the diff, and in the docs — no IDE required.

## Why query and data-frame APIs care

The deeper reason named tuples were stabilized is metaprogramming. Because a named tuple is a first-class *value* of a *structural* type, library authors can inspect and transform its schema at the type level — work that previously demanded macros. A class extending `Selectable` can now expose a `Fields` member typed as a named tuple, and the compiler will resolve `.columnName` selections against it. Combined with type-level helpers like `NamedTuple.From` and `NamedTuple.Map`, that enables **type-safe, ergonomic column APIs**:

```scala
val stats = DataFrame
  .column((words = text.split("\\s+")))
  .withComputed((lowerCase = fun(toLower)(col.words)))
  .groupBy(col.lowerCase)
  .agg(group.key ++ (freq = group.size))
```

Here `col.words` and `col.lowerCase` are checked against the frame's evolving schema — misspell a column and it fails at compile time — and `++` concatenates named tuples to grow the result's shape. This is the pattern behind emerging Scala data-frame and SQL-query libraries: the query result type *is* a named tuple, so the columns you selected are exactly the fields you can access downstream, with no codegen and no stringly-typed lookups.

Named tuples don't replace case classes, and they aren't meant to. They fill the gap between "a raw tuple is too cryptic" and "a class is too heavy" — labeled, structural, allocation-free data that reads like a record and costs like a tuple.

**Try next:** Write `def parseLine(s: String): (key: String, value: Int)` that splits `"a=1"` on `=`, then build a `List[(key: String, value: Int)]`, `filter` it by `.value`, and pattern-match one element with `case (value = v) =>`. Then call `.toTuple` on a result and confirm the labels are gone from the resulting `(String, Int)`.
