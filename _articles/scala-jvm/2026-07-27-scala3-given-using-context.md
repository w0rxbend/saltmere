---
title: "given/using: Scala 3's Principled Take on Implicits"
date: 2026-07-27
track: scala-jvm
summary: "How Scala 3 replaces the overloaded `implicit` keyword with `given`, `using`, `summon`, and extension methods — and how to build a real type class with them."
reading_time: 6
tags: [scala-3, given-using, type-classes, context-bounds, extension-methods]
sources:
  - title: "Contextual Abstractions — Scala 3 Book"
    url: "https://docs.scala-lang.org/scala3/book/ca-contextual-abstractions-intro.html"
  - title: "Context Parameters (using clauses) — Scala 3 Book"
    url: "https://docs.scala-lang.org/scala3/book/ca-context-parameters.html"
  - title: "Type Classes — Scala 3 Book"
    url: "https://docs.scala-lang.org/scala3/book/ca-type-classes.html"
  - title: "Contextual Abstractions — Scala 3 Migration Guide"
    url: "https://docs.scala-lang.org/scala3/guides/migration/incompat-contextual-abstractions.html"
  - title: "Scala release history — endoflife.date"
    url: "https://endoflife.date/scala"
---

Scala 2's `implicit` keyword did too many jobs. The same word marked implicit parameters, implicit conversions, implicit classes, and type-class evidence — four unrelated features sharing one syntax and one reputation for being hard to reason about. Scala 3 splits that overloaded keyword into intent-revealing pieces: `given` to *define* contextual values, `using` to *consume* them, `extension` for methods you bolt onto existing types, and `summon` to fetch an instance by hand. This is verified against Scala 3.8.4 (released June 5, 2026), with 3.3.8 as the current LTS line.

## using: declaring context you need

A `using` clause is a parameter section the caller doesn't have to fill in explicitly — the compiler finds a matching value in scope.

```scala
case class Config(port: Int, host: String)

def renderPage(path: String)(using cfg: Config): String =
  s"http://${cfg.host}:${cfg.port}$path"
```

You can even drop the parameter name if you only pass it downstream:

```scala
def renderWidget(items: List[String])(using Config): String = ???
```

## given: providing it

A `given` defines the canonical value the compiler injects:

```scala
given Config = Config(8080, "saltmere.dev")

renderPage("/home")            // Config supplied automatically
renderPage("/home")(using Config(80, "prod")) // or pass it explicitly
```

This is how you thread cross-cutting context — an `ExecutionContext`, a DB transaction, a request-scoped config — without wiring it through every signature by hand. Standard library APIs like `Future` already take `(using ExecutionContext)`.

## A real type class: Show

Type classes are where contextual abstraction earns its keep. The pattern has three parts: a trait parameterised on the type, `given` instances for each concrete type, and `using` (or a context bound) on functions that need the capability.

```scala
trait Show[A]:
  extension (a: A) def show: String

// instances
given Show[Int] with
  extension (n: Int) def show: String = n.toString

given Show[Boolean] with
  extension (b: Boolean) def show: String = if b then "yes" else "no"

// a derived instance that itself needs a Show[A] in context
given [A](using ev: Show[A]): Show[List[A]] with
  extension (xs: List[A]) def show: String =
    xs.map(_.show).mkString("[", ", ", "]")
```

The `extension` block adds `.show` directly onto the value, so `42.show` and `List(1, 2, 3).show` read naturally at the call site — no wrapper object, no `Show.apply(x).show`.

## Consuming it: context bounds and summon

A function that works for any `A` with a `Show` instance takes it via `using`:

```scala
def describe[A](a: A)(using Show[A]): String =
  s"value = ${a.show}"
```

The `[A: Show]` **context bound** is pure sugar for exactly that `using` parameter:

```scala
def describe[A: Show](a: A): String = s"value = ${a.show}"
```

When you need the instance as a value rather than just calling its methods, `summon` retrieves it explicitly (it replaces Scala 2's `implicitly`):

```scala
def widest[A: Show](xs: List[A]): Int =
  val ev = summon[Show[A]]           // grab the given instance
  xs.map(x => ev.show(x).length).maxOption.getOrElse(0)
```

Putting it together:

```scala
@main def run =
  println(describe(42))              // value = 42
  println(List(true, false).show)    // [yes, no]
```

`List(true, false).show` resolves the derived `Show[List[A]]` instance, which in turn summons `Show[Boolean]` — the compiler composes evidence for you.

## The Scala 2 equivalent, for contrast

The same type class in Scala 2 leaned on `implicit` everywhere, plus an `implicit class` to fake extension methods:

```scala
// Scala 2
trait Show[A] { def show(a: A): String }

implicit val showInt: Show[Int] = new Show[Int] {
  def show(a: Int): String = a.toString
}

def describe[A](a: A)(implicit s: Show[A]): String = "value = " + s.show(a)

implicit class ShowOps[A](a: A)(implicit s: Show[A]) {
  def show: String = s.show(a)
}
```

Functionally identical, but the reader has to infer *which* role each `implicit` is playing.

## The migration story

The mapping is mechanical, and the compiler helps:

- `implicit val` / `implicit def` (a value) → `given`
- `implicit` parameter → `using`
- `implicitly[T]` → `summon[T]`
- `implicit class` → `extension` methods
- `implicit def` used for conversion → `given Conversion[A, B]` (and you must `import scala.language.implicitConversions`)

Crucially, Scala 3 still *accepts* the old `implicit` syntax, so mixed codebases compile during a transition. The `-rewrite` compiler flag combined with `-source 3.0-migration` can auto-rewrite much of it. The migration guide flags the sharp edges — chiefly that implicit conversions are now opt-in and that given resolution is slightly stricter about ambiguity, which surfaces latent bugs rather than silently picking a winner.

The net effect: the mechanism that used to be Scala's most feared feature now announces its intent in the syntax. `given` defines, `using` requires, `summon` fetches, `extension` decorates — four words for four jobs the one `implicit` keyword used to blur together.

**Try next:** Write a `JsonEncoder[A]` type class with `given` instances for `Int`, `String`, and `List[A]`, add an `extension (a: A) def toJson`, then define `encode[A: JsonEncoder](a: A): String` and confirm that `List(1, 2, 3).toJson` resolves the derived `List` instance by composing the element encoder.
