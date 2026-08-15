---
title: "given/using: Scala 3's Principled Take on Implicits"
date: 2026-07-27
track: scala-jvm
summary: "How Scala 3 replaces the overloaded `implicit` keyword with `given`, `using`, `summon`, and extension methods, and how a type class is built from them."
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

**Gist.** Scala 2 spelled four unrelated features — implicit parameters, implicit conversions, implicit classes, and type-class evidence — with the single keyword `implicit`, so the role of a definition could not be read off its syntax. Scala 3 replaces that one keyword with distinct constructs: `given` defines a contextual value, `using` declares a parameter section the compiler fills in, `extension` attaches methods to an existing type, and `summon` retrieves an instance as a value. The cost is that argument selection remains a compile-time search over the terms in scope: the search is now stricter about ambiguity, so code that previously compiled by silently picking one candidate may fail to compile instead. The syntax below is the Scala 3 contextual-abstraction syntax described in the Scala 3 Book; the 3.3.x series is the long-term support (LTS) line.

## using: declaring the context a definition requires

A `using` clause is **a parameter section the caller may omit**; the compiler supplies an argument by searching for a term of the required type in the implicit scope.

```scala
case class Config(port: Int, host: String)

def renderPage(path: String)(using cfg: Config): String =
  s"http://${cfg.host}:${cfg.port}$path"
```

The parameter name may be dropped when the value is only forwarded to another `using` clause rather than named in the body:

```scala
def renderWidget(items: List[String])(using Config): String = ???
```

## given: providing the value

A `given` introduces the value the compiler injects for a matching `using` clause.

```scala
given Config = Config(8080, "saltmere.dev")

renderPage("/home")            // Config supplied automatically
renderPage("/home")(using Config(80, "prod")) // or passed explicitly
```

Two properties are load-bearing. **The argument is resolved by type, not by name**, which is why the name may be omitted at both definition and use site. And **an explicit `(using …)` argument list always wins over the search**, so a caller can override the ambient value without shadowing the `given`.

This is the mechanism that threads cross-cutting context — an `ExecutionContext`, a database transaction, a request-scoped configuration — without adding an explicit parameter to every intermediate signature. Standard library APIs use it directly: `Future.apply` takes an `ExecutionContext` as a context parameter rather than as an ordinary argument.

## A type class: Show

The type-class encoding has three parts: a trait parameterised on the type, a `given` instance per concrete type, and a `using` clause (or context bound) on every function that needs the capability.

```scala
trait Show[A]:
  extension (a: A) def show: String

// instances
given Show[Int] with
  extension (n: Int) def show: String = n.toString

given Show[Boolean] with
  extension (b: Boolean) def show: String = if b then "yes" else "no"

// a derived instance that itself requires a Show[A] in context
given [A](using ev: Show[A]): Show[List[A]] with
  extension (xs: List[A]) def show: String =
    xs.map(_.show).mkString("[", ", ", "]")
```

The `extension` block puts `.show` on the value itself, so `42.show` and `List(1, 2, 3).show` read as method calls with **no wrapper object and no `Show.apply(x).show` indirection**.

The `Show[List[A]]` instance is the interesting case: it is **a parameterised given, that is, a rule rather than a value**. Resolving `List(true, false).show` requires the compiler to select that rule and then discharge its own `using ev: Show[A]` obligation with `Show[Boolean]`. Evidence composes recursively, and the composition happens at compile time — the failure mode is a compile error naming the type whose instance is missing, not a runtime lookup miss.

## Consuming evidence: context bounds and summon

A function polymorphic in `A` but requiring a `Show[A]` takes it through `using`:

```scala
def describe[A](a: A)(using Show[A]): String =
  s"value = ${a.show}"
```

The `[A: Show]` **context bound is sugar for exactly that `using` parameter** — the same signature after desugaring:

```scala
def describe[A: Show](a: A): String = s"value = ${a.show}"
```

A context bound leaves the parameter anonymous. When the instance is needed as a value rather than as a receiver of extension methods, `summon` retrieves it; it is the Scala 3 replacement for Scala 2's `implicitly`.

```scala
def widest[A: Show](xs: List[A]): Int =
  val ev = summon[Show[A]]           // the given instance, as a value
  xs.map(x => ev.show(x).length).maxOption.getOrElse(0)
```

```scala
@main def run =
  println(describe(42))              // value = 42
  println(List(true, false).show)    // [yes, no]
```

### Implementation sketch (Scala)

A second type class shows the same three parts and makes the recursive derivation explicit: the instance for `List[A]` is selected only when an instance for the element type is already resolvable.

```scala
trait JsonEncoder[A]:
  extension (a: A) def toJson: String

given JsonEncoder[Int] with
  extension (n: Int) def toJson: String = n.toString

given JsonEncoder[String] with
  extension (s: String) def toJson: String =
    "\"" + s.replace("\\", "\\\\").replace("\"", "\\\"") + "\""

given [A](using JsonEncoder[A]): JsonEncoder[List[A]] with
  extension (xs: List[A]) def toJson: String =
    xs.map(_.toJson).mkString("[", ",", "]")

given [A](using JsonEncoder[A]): JsonEncoder[Option[A]] with
  extension (o: Option[A]) def toJson: String =
    o.map(_.toJson).getOrElse("null")

def encode[A: JsonEncoder](a: A): String = a.toJson

@main def demo =
  println(encode(List(1, 2, 3)))                 // [1,2,3]
  println(encode(List(Some("a"), None)))         // ["a",null]
```

`encode(List(Some("a"), None))` requires `JsonEncoder[List[Option[String]]]`, which the compiler builds by applying the `List` rule, then the `Option` rule, then the `String` instance. Removing the `String` instance turns the expression into a compile error rather than a runtime failure.

## The Scala 2 encoding, for contrast

The same type class in Scala 2 used `implicit` for every role, with an `implicit class` standing in for extension methods:

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

The behaviour is the same; the role each `implicit` plays has to be inferred from position rather than read from the keyword.

## Migration

The mapping is mechanical:

- `implicit val` / `implicit def` defining a value → `given`
- `implicit` parameter → `using`
- `implicitly[T]` → `summon[T]`
- `implicit class` → `extension` methods
- `implicit def` used as a conversion → `given Conversion[A, B]`, which additionally requires `import scala.language.implicitConversions`

Scala 3 **still accepts the Scala 2 `implicit` syntax**, so a mixed codebase compiles during a transition. The `-rewrite` compiler flag combined with `-source 3.0-migration` rewrites much of the old syntax automatically. The migration guide identifies the sharp edges: implicit conversions are now opt-in, and given resolution is stricter about ambiguity, which turns a previously silent choice between candidates into a compile error.

## Pitfalls

- **Two `given` instances of the same type in scope produce an ambiguity error at the use site, not at the definition.** The error names the call that needed the evidence, so the offending import is often in a different file from the reported line.
- **`implicit def` conversions no longer compile without `import scala.language.implicitConversions`.** The symptom after migration is a feature-warning-turned-error on a file that changed in no other way.
- **A context bound gives no name to the instance.** Code that needs the instance as a value inside such a method must call `summon[Show[A]]`; referring to a parameter name that the desugaring never created is a compile error.
- **A missing element instance surfaces as a failure on the container type.** `List(1, 2, 3).toJson` reports no `JsonEncoder[List[Int]]` when the actual gap is `JsonEncoder[Int]`, because the derived rule cannot discharge its own `using` obligation.
- **An explicit `(using …)` argument overrides the ambient `given` silently.** A call site that passes its own value keeps compiling after the surrounding `given` is changed, so configuration changes appear to have no effect on that path.
- **Extension methods only apply when the instance is resolvable at the call site.** Moving a `given` out of the companion object of its type removes it from the implicit scope, and `.show` stops resolving even though the trait and instance both still exist.
