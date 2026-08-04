---
title: "Scala 3 Match Types: Pattern Matching That Runs in the Type Checker"
date: 2026-08-04
track: scala-jvm
summary: "Match types let a type reduce to a different type based on the shape of its scrutinee. This is type-level pattern matching, with recursion, standard-library integration through Tuple, and a few sharp edges around reduction and explicit bounds."
reading_time: 6
tags: [scala3, match-types, type-level-programming, tuple, dependent-types, jvm]
sources:
  - title: "Match Types — Scala 3 Reference"
    url: "https://docs.scala-lang.org/scala3/reference/new-types/match-types.html"
  - title: "SIP-56: Proper Specification for Match Types"
    url: "https://docs.scala-lang.org/sips/match-types-spec.html"
  - title: "scala.Tuple (standard library source)"
    url: "https://github.com/scala/scala3/blob/main/library/src/scala/Tuple.scala"
  - title: "Match Types in Scala 3 — Baeldung on Scala"
    url: "https://www.baeldung.com/scala/match-types"
  - title: "Scala 3: Match Types Quickly Explained — Rock the JVM"
    url: "https://rockthejvm.com/articles/scala-3-match-types"
---

A match type is a type that *reduces* to another type based on the shape of a scrutinee type. Where an ordinary `match` inspects a value at runtime, a match type inspects a type at compile time and picks a right-hand side. The mechanism is small, but it is the substrate the standard library uses to give `Tuple` its statically known length and element types, and it is how you write functions whose return type genuinely depends on the argument type. Everything here compiles on current Scala (3.8.4, or the 3.3.8 LTS line).

## The shape of a match type

The canonical example from the reference lifts "give me the element type of X" into the type system:

```scala
type Elem[X] = X match
  case String      => Char
  case Array[t]    => t
  case Iterable[t] => t
```

`Elem[String]` reduces to `Char`, `Elem[Array[Int]]` to `Int`, and `Elem[List[Float]]` to `Float`. You can force reduction and check it at the type level:

```scala
summon[Elem[String]        =:= Char]
summon[Elem[Array[Double]] =:= Double]
summon[Elem[List[Int]]     =:= Int]
```

Each `case` has a pattern on the left and a body type on the right. A pattern like `Array[t]` introduces a type variable `t` that binds to whatever the scrutinee's argument is, exactly like a value-level constructor pattern binds a field.

## How reduction actually works

The compiler tries the cases top to bottom. For a case `case P => T`, reduction to `T` happens when the scrutinee is provably a subtype of `P` (with the pattern's type variables instantiated as needed). If the scrutinee is *not* a subtype, the compiler tries to prove the scrutinee and `P` are disjoint; only if that succeeds does it move to the next case. This is why order matters: `Iterable[t]` sits last because `Array` is not an `Iterable`, but many things are, so the specific cases go first.

Two details bite people. First, type variables are instantiated *minimally*: a variable that appears covariantly in the body is chosen as small as possible, one that appears contravariantly as large as possible. Second, reduction is a compile-time search, not a runtime dispatch — if the compiler cannot decide subtyping or disjointness for an abstract scrutinee, the type simply does not reduce, and it stays as the unreduced `Elem[X]` until more is known.

## Dependent methods

The reason match types earn their keep is that a method can return one. The reliable pattern is a method of shape `def f[X](x: X): MatchType[X]`, whose value-level `match` mirrors the type-level one case for case:

```scala
def firstElement[X](xs: X): Elem[X] = xs match
  case s: String      => s.charAt(0)
  case a: Array[t]    => a(0)
  case i: Iterable[t] => i.head
```

In the `String` branch the expected type is `Elem[String]`, which reduces to `Char`, and `charAt` returns `Char` — so it type-checks. `firstElement("hi")` has static type `Char`; `firstElement(List(1, 2, 3))` has static type `Int`. No cast, no `Any`. The compiler flows the scrutinee refinement into each branch and reduces the return type accordingly.

## Recursion

Match types may reference themselves, which is where they become a small functional language over types. A type-level length over tuples looks exactly like the recursive value function you would write:

```scala
import scala.compiletime.ops.int.S

type LengthOf[X <: Tuple] <: Int = X match
  case EmptyTuple => 0
  case _ *: xs    => S[LengthOf[xs]]
```

A tuple type `(String, Int, Boolean)` *is* `String *: Int *: Boolean *: EmptyTuple`, so `LengthOf` peels one element per step and adds one via `S`, the compile-time successor on singleton `Int` types:

```scala
summon[LengthOf[(String, Int, Boolean)] =:= 3]
summon[LengthOf[EmptyTuple]              =:= 0]
```

The compiler has cycle detection and will reject genuinely non-terminating definitions rather than looping forever, but it does not prove termination in general — keep recursive cases structurally decreasing, as above.

## Match-type bounds

Look closely at the declaration above: `type LengthOf[X <: Tuple] <: Int`. That trailing `<: Int` is a *match-type bound*, and it is not decoration. Consider a type-level concatenation:

```scala
type Concat[Xs <: Tuple, Ys <: Tuple] <: Tuple = Xs match
  case EmptyTuple => Ys
  case x *: xs    => x *: Concat[xs, Ys]
```

In the recursive case, `x *: Concat[xs, Ys]` uses `*:`, whose right operand must be a `Tuple`. But when `xs` is an abstract type variable the compiler cannot reduce `Concat[xs, Ys]` to a concrete tuple, so on its own it does not know the recursive call yields a `Tuple`, and the body fails to type-check. The declared bound `<: Tuple` rescues this: every instance of `Concat`, reduced or not, is known to conform to `Tuple`, which is enough for `*:` to accept it. Drop the bound and you get a compile error inside the definition. The rule of thumb: if a recursive match type is used in a position that constrains it (a `*:` operand, an `Int` literal via `S`, a bounded type parameter elsewhere), give it an explicit upper bound.

## The Tuple connection

None of this is academic; it is how `scala.Tuple` is built. The standard library defines these as match types almost verbatim to what you would write by hand:

```scala
type Size[X <: Tuple] <: Int = X match
  case EmptyTuple => 0
  case x *: xs    => S[Size[xs]]

type Concat[X <: Tuple, +Y <: Tuple] <: Tuple = X match
  case EmptyTuple => Y
  case x1 *: xs1  => x1 *: Concat[xs1, Y]

type Map[Tup <: Tuple, F[_ <: Union[Tup]]] <: Tuple = Tup match
  case EmptyTuple => EmptyTuple
  case h *: t     => F[h] *: Map[t, F]
```

`Tuple.Map` is the one to remember: it applies a type constructor to every element type. `Tuple.Map[(Int, String), List]` reduces to `(List[Int], List[String])`. That single match type is what lets libraries transform heterogeneous tuples — wrapping every field in `Option`, `Future`, or a decoder — while keeping the exact static shape. `Tuple.Head`, `Tuple.Elem`, and `Tuple.Size` are all the same idea, and they are why a Scala 3 tuple knows its own arity and per-position types at compile time.

## Pitfalls

The error you will meet most is *"Match type reduction failed since selector Float matches none of the cases."* It means the scrutinee matched no pattern and none could be proven disjoint, so reduction is stuck. If a total function is intended, add a catch-all `case _ => T`. If not, the message is telling you the call site is genuinely unsupported.

Beyond that: cases are tried in order and the first subtype match wins, so an overly general case placed early silently shadows later ones — order from specific to general. Match-type cases cannot carry guards; push any conditional logic into the value-level branch, not the type. And the dependent-method trick only reliably works for the `def f[X](x: X): M[X]` shape — try to thread a match-typed *parameter* through arithmetic or accumulation and the compiler will not flow enough information to type-check the operations, because it cannot see which case reduced.

Match types trade a little compile-time patience for return types that are precise instead of `Any`. Reach for them when a function's result type is a function of its input type, and lean on `Tuple`'s built-in match types before rolling your own.

**Try next:** Write `type Flatten[X <: Tuple] <: Tuple` that flattens one level of nested tuples, then compare your version against `Tuple.FlatMap` in the standard library source.
