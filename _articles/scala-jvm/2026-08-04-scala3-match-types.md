---
title: "Scala 3 Match Types: Pattern Matching That Runs in the Type Checker"
date: 2026-08-04
track: scala-jvm
summary: "Match types let a type reduce to a different type based on the shape of its scrutinee. This is type-level pattern matching, with recursion, standard-library integration through Tuple, and sharp edges around reduction and explicit bounds."
reading_time: 7
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

**Gist.** A function whose result type depends on its argument type cannot be given a useful signature with ordinary generics; the fallback is a widened return type such as `Any` plus a cast at every call site. A **match type** removes the widening: it is a type that *reduces* to another type by pattern-matching on the shape of a scrutinee type at compile time, so `def f[X](x: X): M[X]` reports a different static result type for each input type. The cost is that reduction is a compile-time subtyping-and-disjointness search which can get stuck on abstract scrutinees, and recursive match types frequently require an explicit declared upper bound before their own bodies type-check.

Where an ordinary value-level `match` inspects a value at run time, a match type inspects a type during type checking and selects a right-hand side. The mechanism is small, but it is the substrate the standard library uses to give `Tuple` a statically known length and per-position element types. Match types have been part of the language since the first Scala 3 release, and SIP-56 gives them a specification independent of any one compiler version.

## The shape of a match type

The canonical example from the Scala 3 reference lifts "the element type of X" into the type system:

```scala
type Elem[X] = X match
  case String      => Char
  case Array[t]    => t
  case Iterable[t] => t
```

`Elem[String]` reduces to `Char`, `Elem[Array[Int]]` to `Int`, and `Elem[List[Float]]` to `Float`. Reduction can be forced and checked at the type level:

```scala
summon[Elem[String]        =:= Char]
summon[Elem[Array[Double]] =:= Double]
summon[Elem[List[Int]]     =:= Int]
```

Each `case` has a pattern on the left and a body type on the right. A pattern such as `Array[t]` introduces a type variable `t` bound to the scrutinee's type argument, in the same way a value-level constructor pattern binds a field.

## How reduction proceeds

The compiler tries the cases top to bottom. For a case `case P => T`, **reduction to `T` occurs when the scrutinee is provably a subtype of `P`**, with the pattern's type variables instantiated as needed. If the scrutinee is not a subtype, the compiler attempts to prove that the scrutinee and `P` are **disjoint**; only when that proof succeeds does it advance to the next case. Order therefore carries meaning: `Iterable[t]` sits last because `Array` is not an `Iterable` while many other types are, so the specific cases must precede the general one.

Two properties of the search are load-bearing. First, **the pattern's type variables are instantiated by the same subtyping check that decides the case**: the instantiation is whatever makes the scrutinee conform to the pattern, so `Array[t]` against `Array[Int]` fixes `t = Int`. Second, **reduction is a compile-time search, not a run-time dispatch**. When neither subtyping nor disjointness can be decided for an abstract scrutinee, the type does not reduce at all; it remains the unreduced application `Elem[X]` until further information arrives.

## Dependent methods

The practical payoff is that a method may return a match type. The reliable shape is `def f[X](x: X): MatchType[X]`, whose value-level `match` mirrors the type-level one case for case:

```scala
def firstElement[X](xs: X): Elem[X] = xs match
  case s: String      => s.charAt(0)
  case a: Array[t]    => a(0)
  case i: Iterable[t] => i.head
```

In the `String` branch the expected type is `Elem[String]`, which reduces to `Char`, and `charAt` returns `Char`, so the branch type-checks. `firstElement("hi")` has static type `Char`; `firstElement(List(1, 2, 3))` has static type `Int`. No cast and no `Any` appear. The compiler flows the scrutinee refinement of each branch into the expected type and reduces it accordingly.

## Recursion

Match types may reference themselves, which makes them a small functional language over types. A type-level length over tuples has the structure of the corresponding recursive value function:

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

**The compiler does not prove termination**; it bounds the work instead, aborting reduction with an error once a recursion limit is exceeded rather than looping indefinitely. Recursive cases must therefore be kept structurally decreasing as above.

## Match-type bounds

The declaration above reads `type LengthOf[X <: Tuple] <: Int`. The trailing `<: Int` is a **match-type bound**, and it is not decoration. Consider a type-level concatenation:

```scala
type Concat[Xs <: Tuple, Ys <: Tuple] <: Tuple = Xs match
  case EmptyTuple => Ys
  case x *: xs    => x *: Concat[xs, Ys]
```

In the recursive case, `x *: Concat[xs, Ys]` uses `*:`, whose right operand must be a `Tuple`. When `xs` is an abstract type variable the compiler cannot reduce `Concat[xs, Ys]` to a concrete tuple, so on its own it does not know the recursive application yields a `Tuple`, and the body fails to type-check. **The declared bound `<: Tuple` supplies exactly that knowledge: every instance of `Concat`, reduced or not, conforms to `Tuple`**, which is sufficient for `*:` to accept it. Removing the bound produces a compile error inside the definition itself, not at the call site. The general rule: a recursive match type used in a position that constrains it — a `*:` operand, an `Int` singleton fed to `S`, a bounded type parameter elsewhere — needs an explicit upper bound.

## The Tuple connection

These constructions are how `scala.Tuple` is built. The standard library defines the following as match types:

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

`Tuple.Map` applies a type constructor to every element type: `Tuple.Map[(Int, String), List]` reduces to `(List[Int], List[String])`. That single match type is what allows libraries to transform heterogeneous tuples — wrapping every field in `Option`, `Future`, or a decoder — while preserving the exact static shape. `Tuple.Head`, `Tuple.Elem` and `Tuple.Size` follow the same pattern, and together they are why a Scala 3 tuple carries its arity and per-position types at compile time.

### Implementation sketch (Scala)

A type-level `Map` is only useful when a value-level operation is indexed by it. The sketch below shows the two halves: `Tuple.map`, whose result type is `Tuple.Map`, and an inline recursion that walks the same tuple shape to collect one type-class instance per element type.

```scala
import scala.compiletime.{erasedValue, summonInline}

trait Wrap[A]:
  type Out
  def apply(a: A): Out

// The result type is computed, not widened: (Int, String) => (Option[Int], Option[String])
def wrapAll[T <: Tuple](t: T): Tuple.Map[T, Option] =
  t.map[Option]([X] => (x: X) => Option(x))

// Recursive summoning: one instance per element type, driven by the tuple's shape.
inline def summonAll[T <: Tuple]: List[Any] =
  inline erasedValue[T] match
    case _: EmptyTuple => Nil
    case _: (h *: t)   => summonInline[Wrap[h]] :: summonAll[t]
```

The `inline match` on `erasedValue[T]` is the value-level counterpart of the type-level recursion: **each step strips one `h *: t` layer, and the compiler unrolls the recursion because the tuple's length is statically known**. Without the match type, the return type of `wrapAll` would have to widen to `Tuple`, discarding every element type.

## Pitfalls

- *"Match type reduction failed since selector Float matches none of the cases."* The scrutinee matched no pattern and none could be proven disjoint, so reduction is stuck; a total function needs an explicit catch-all `case _ => T`, otherwise the message reports a genuinely unsupported call site.
- Cases are tried in order and the first subtype match wins, so a general case placed early shadows every later case silently — no warning marks the unreachable ones.
- Match-type cases cannot carry guards; conditional logic must live in the value-level branch, because the type-level pattern has no place to express it.
- The dependent-method behaviour is reliable for the shape `def f[X](x: X): M[X]`. Threading a match-typed *parameter* through arithmetic or accumulation fails to type-check, because the compiler cannot see which case reduced and therefore cannot justify the operations.
- Reduction on an abstract type parameter leaves the application unreduced rather than raising an error; the failure surfaces later, at the point where the unreduced type is required to conform to something concrete.
