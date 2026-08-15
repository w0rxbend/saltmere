---
title: "Scala 3 inline: compile-time metaprogramming without macros"
date: 2026-08-15
track: scala-jvm
summary: "Scala 3's inline is a guarantee rather than a hint, and it carries a compile-time evaluation layer: inline match for specialization, transparent inline for refined result types, constValue and erasedValue for computing over types, and compiletime.error for custom compiler errors. Work that required a Scala 2 macro often requires none of the quotes/splices machinery."
reading_time: 6
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

**Gist.** Generating specialized code from a static description — a field list, a literal exponent, a tuple of types — traditionally required a macro system that manipulates syntax trees. Scala 3 makes `inline` a language guarantee rather than an optimizer hint: an `inline def` call site is **always** replaced by the definition's body during compilation, so the compiler is guaranteed to be looking at expanded code and can be asked to reduce conditionals, branch on types, narrow result types, and abort with a custom message. The cost is that everything the mechanism consumes must be statically reducible: an argument that is not a compile-time constant, or a scrutinee that does not reduce, is a compilation error rather than a fallback to runtime behaviour.

In C, `inline` is a suggestion the compiler may ignore. In Scala 3 it is part of the semantics of the definition, and the same firm boundary that makes expansion predictable is what turns it into a metaprogramming tool. A large share of the work that needed `def macro` in Scala 2 — and that [match types](/articles/scala-jvm/2026-08-04-scala3-match-types) express at the type level — is expressible as ordinary definitions.

## inline def and inline parameters

```scala
inline def logged[T](inline label: String)(op: => T): T =
  println(s"start: $label")
  try op finally println(s"end: $label")
```

Every call to `logged` is expanded in place, so no call is dispatched at runtime. Marking a *parameter* `inline` changes the argument's treatment as well — the argument expression **is substituted verbatim at each use site instead of being bound to a local**, so a constant argument remains a constant inside the expansion. That property is what the remaining constructs depend on. `inline val` similarly guarantees a compile-time constant.

## inline if and inline match: enforced static reduction

Inside an inline definition, `inline if` and `inline match` must be **decided at compile time**. The condition, or the scrutinee's type, has to reduce statically, and **only the selected branch is code-generated**:

```scala
inline def power(x: Double, inline n: Int): Double =
  inline if n == 0 then 1.0
  else inline if n % 2 == 1 then x * power(x, n - 1)
  else { val y = power(x, n / 2); y * y }

power(v, 10)   // expands to a chain of multiplications; no loop, no recursion
power(v, k)    // error: k is not a compile-time constant
```

The second line failing is the mechanism working. Specialization in the style of C++ templates is available, with an **enforced boundary between what is known during compilation and what is known at runtime**; there is no silent degradation to a runtime loop.

## transparent inline: result types that narrow

A plain inline definition keeps its declared result type. A **`transparent inline`** definition's result type is instead **narrowed to the type of whatever the expansion produced at that call site**:

```scala
transparent inline def defaultOf(inline kind: String): Any =
  inline kind match
    case "int"    => 0
    case "string" => ""

val n: Int    = defaultOf("int")     // typed Int, not Any
val s: String = defaultOf("string")  // typed String
```

The declared type is `Any`, yet each call site is typed by its own branch. This is the mechanism behind methods that appear uniform in their signature while returning call-site-specific types.

## The scala.compiletime toolkit

`scala.compiletime` supplies the primitives for computing over types:

- **`constValue[T]`** — extracts the value of a singleton or literal type; `constValue[3]` is `3`, and `constValue[Tuple.Size[T]]` yields a tuple's arity.
- **`erasedValue[T]`** — a value with no runtime representation, usable **only as the scrutinee of an `inline match`**, which permits matching on a type where no value of it exists.
- **`error("msg")`** — aborts compilation with the given message; `codeOf(expr)` renders an expression's source text for inclusion in it.
- **`scala.compiletime.ops`** — type-level operations such as `int.+`, `int.<`, `string.+`.

### Implementation sketch (Scala)

Recursion over a **tuple of types** with `erasedValue` is the canonical type-level loop. The outer match destructures the tuple type into head and tail; the inner match inspects the head type and either folds its literal value or aborts with a message written by the author of the definition rather than by the implicit-search machinery.

```scala
import scala.compiletime.{erasedValue, constValue, error}

inline def sumOf[T <: Tuple]: Int =
  inline erasedValue[T] match
    case _: EmptyTuple => 0
    case _: (h *: t)   =>
      inline erasedValue[h] match
        case _: Int => constValue[h & Int] + sumOf[t]
        case _      => error("sumOf only supports Int literal tuples")

val six: Int = sumOf[(1, 2, 3)]   // expands to 1 + 2 + 3; the recursion is gone
```

The `h & Int` intersection is load-bearing: the outer pattern binds `h` as the head type without constraining it, and only the inner match establishes that it is an `Int`, so `constValue` needs the refined type to extract the literal. The same shape drives `Mirror`-based [typeclass derivation](/articles/scala-jvm/2026-08-07-scala3-typeclass-derivation-mirror) over a case class's field types.

## Where quoted macros remain necessary

| Need | Construct |
|---|---|
| Guaranteed inlining, specialization | `inline def` / `inline match` |
| Call-site-refined result types | `transparent inline` |
| Computing over literal or tuple types | `constValue` / `erasedValue` / `ops` |
| Custom compile errors | `compiletime.error` |
| Inspecting or building *expression trees* | quotes `'{ }` / splices `${ }` |

The boundary is precise: inline can select branches, substitute arguments and fold constants, but **it cannot inspect the syntax tree of an argument**. Distinguishing a caller that wrote `x.name` from one that wrote `x.age` — deriving serialization from field access, building SQL from an expression, validating the internal structure of a format string — requires quoted macros, written as `inline def f(x: T) = ${ fImpl('x) }`. Inline remains the entry point in that case: **every Scala 3 macro is an inline definition whose body is a splice.**

## Pitfalls

- Passing a runtime value where an `inline` parameter is expected fails compilation with no runtime fallback; a definition that must accept both needs a separate non-inline overload.
- An `inline match` whose scrutinee type does not reduce to a single case reports that it cannot be reduced, rather than deferring the decision to runtime.
- Recursive inline definitions expand eagerly, so an argument that grows the recursion — a large literal exponent, a long tuple — inflates generated code and compilation time at every call site.
- `erasedValue[T]` is an erased definition, so the compiler rejects it in any position where an actual value would be required rather than producing something that fails at runtime.
- `transparent inline` widens nothing, so a definition whose branches return unrelated types leaks those types into inferred signatures downstream; changing a branch's type is a source-compatibility change for callers that relied on the narrowed type.
- Changing the body of an `inline def` in a library requires recompiling its callers, because the previous body was copied into their bytecode.
