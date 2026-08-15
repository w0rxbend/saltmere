---
title: "Scala 3 inline: compile-time metaprogramming without macros"
date: 2026-08-15
track: scala-jvm
summary: "Scala 3's inline is not a performance hint — it's a guarantee, and it comes with a whole compile-time evaluation layer: inline match for zero-cost specialization, transparent inline for refined result types, constValue and erasedValue for computing over types, and compiletime.error for custom compiler errors. Most jobs that needed a Scala 2 macro now need none of the quotes/splices machinery at all."
reading_time: 5
tags: [scala3, inline, metaprogramming, compiletime, macros, typelevel]
sources:
  - title: "Inline — Scala 3 Reference: Metaprogramming"
    url: "https://docs.scala-lang.org/scala3/reference/metaprogramming/inline.html"
  - title: "Compile-time operations — Scala 3 Reference: Metaprogramming"
    url: "https://docs.scala-lang.org/scala3/reference/metaprogramming/compiletime-ops.html"
  - title: "Inline — Macros in Scala 3 guide"
    url: "https://docs.scala-lang.org/scala3/guides/macros/inline.html"
  - title: "Scala Compile-time Operations — Macros in Scala 3 guide"
    url: "https://docs.scala-lang.org/scala3/guides/macros/compiletime.html"
  - title: "scala.compiletime — API documentation"
    url: "https://www.scala-lang.org/api/current/scala/compiletime.html"
---

In C, `inline` is a suggestion the compiler is free to ignore. In Scala 3, **`inline` is a language guarantee**: the call site is *always* replaced by the definition's body at compile time, or compilation fails. That firm semantics is what makes it a metaprogramming tool rather than an optimizer knob — because once the compiler is guaranteed to be looking at the expanded code, it can be asked to *evaluate* parts of it, branch on types, refine result types, and emit custom errors. A large slice of what needed `def macro` machinery in Scala 2 (and what [match types](/articles/scala-jvm/scala3-match-types) do at the type level) is now plain, readable code.

## inline def and inline parameters

```scala
inline def logged[T](inline label: String)(op: => T): T =
  println(s"start: $label")
  try op finally println(s"end: $label")
```

Every call to `logged` is expanded in place — no `Function0` allocation for the by-name, no megamorphic call. Marking the *parameter* `inline` goes further: the argument expression must be statically known where required, and it is substituted verbatim at each use site instead of being bound to a local. Constants stay constants through the expansion, which is what powers everything below. (`inline val`, similarly, guarantees a compile-time constant.)

## inline if / match: specialization

Inside an inline def, `inline if` and `inline match` must be **decided at compile time** — the condition or scrutinee has to reduce statically, and only the chosen branch is ever code-generated:

```scala
inline def power(x: Double, inline n: Int): Double =
  inline if n == 0 then 1.0
  else inline if n % 2 == 1 then x * power(x, n - 1)
  else { val y = power(x, n / 2); y * y }

power(v, 10)   // compiles to the multiplication chain — no loop, no recursion
power(v, k)    // error: k is not a compile-time constant
```

That error is the feature: you get C++-template-style specialization with an enforced boundary between "known now" and "known at runtime".

## transparent inline: types that sharpen

A normal inline def keeps its declared result type. A **`transparent inline`** def's result type is *narrowed to the type of whatever the expansion produced*:

```scala
transparent inline def defaultOf(inline kind: String): Any =
  inline kind match
    case "int"    => 0
    case "string" => ""

val n: Int    = defaultOf("int")     // typed Int, not Any
val s: String = defaultOf("string")  // typed String
```

Declared `Any`, but each call site gets the precise branch type. This is how libraries return refined, call-site-specific types from what looks like one method — the trick behind much of the ergonomics in named tuples and typeclass derivation.

## The scala.compiletime toolkit

`scala.compiletime` supplies the primitives for computing over types:

- **`constValue[T]`** — extract the value of a singleton/literal type: `constValue[3]` is `3`, `constValue[Tuple.Size[T]]` counts a tuple.
- **`erasedValue[T]`** — a phantom value usable *only* as an `inline match` scrutinee, letting you pattern-match on a type with no runtime value.
- **`error("msg")`** — abort compilation with your own message; `codeOf(expr)` prints the expression into it.
- **`scala.compiletime.ops`** — type-level arithmetic like `int.+`, `int.<`, `string.+`.

Recursing over a **tuple of types** with `erasedValue` is the canonical typelevel loop:

```scala
import scala.compiletime.{erasedValue, constValue, error}

inline def sumOf[T <: Tuple]: Int =
  inline erasedValue[T] match
    case _: EmptyTuple => 0
    case _: (h *: t)   =>
      inline erasedValue[h] match
        case _: Int => constValue[h & Int] + sumOf[t]
        case _      => error("sumOf only supports Int literal tuples")

val six: 6 = sumOf[(1, 2, 3)]   // computed entirely at compile time
```

This is exactly the pattern `Mirror`-based [typeclass derivation](/articles/scala-jvm/scala3-typeclass-derivation-mirror) uses to walk a case class's field types — no macro in sight, and the failure mode is a *readable custom error*, not an implicit-not-found wall.

## When you still want real macros

| Need | Reach for |
|---|---|
| Guaranteed inlining, specialization | `inline def` / `inline match` |
| Call-site-refined result types | `transparent inline` |
| Computing over literal/tuple types | `constValue` / `erasedValue` / `ops` |
| Custom compile errors | `compiletime.error` |
| Inspecting or building *expression trees* | quotes `'{ }` / splices `${ }` |

The boundary is crisp: inline can select, substitute, and fold constants, but it cannot *look inside* an argument's syntax tree. The moment you need to know that the caller wrote `x.name` and not `x.age` — serialization from field access, SQL from an expression, build-time validation of a format string's *structure* — you cross into quoted macros (`inline def f(x: T) = ${ fImpl('x) }`). Even then, `inline` remains the front door: every Scala 3 macro is an inline def whose body is a splice. Start with inline; escalate only when the compiler tells you it can't reduce.

**Try next:** rewrite a runtime `require(n > 0)` in one of your own libraries as `inline def` + `compiletime.error("n must be positive")` over a literal parameter, and watch invalid calls fail at compile time with your message.
