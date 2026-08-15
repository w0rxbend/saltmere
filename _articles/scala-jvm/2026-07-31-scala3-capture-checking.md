---
title: "Scala 3 Capture Checking: Keeping Capabilities From Escaping"
date: 2026-07-31
track: scala-jvm
summary: "Scala 3's experimental capture checking tracks which capabilities a value holds so the compiler can reject a file handle, logger, or effect that outlives its scope. The model, an escaping-resource example, and the flags that enable it."
reading_time: 6
tags: [scala3, capture-checking, capabilities, effects, type-system, experimental]
sources:
  - title: "Capture Checking (Scala 3 reference)"
    url: "https://docs.scala-lang.org/scala3/reference/experimental/cc.html"
  - title: "Capture Checking Basics (nightly docs)"
    url: "https://nightly.scala-lang.org/docs/reference/experimental/capture-checking/basics.html"
  - title: "Capturing Types (Boruch-Gruszecki, Odersky et al., ACM TOPLAS 2023)"
    url: "https://dl.acm.org/doi/10.1145/3618003"
  - title: "Scala 3.8 released!"
    url: "https://www.scala-lang.org/news/3.8/"
  - title: "Understanding Capture Checking in Scala (SoftwareMill)"
    url: "https://softwaremill.com/understanding-capture-checking-in-scala/"
---

**Gist.** A large class of Java Virtual Machine (JVM) resource bugs are lifetime bugs: a `FileOutputStream`, database connection, or logger is captured by a closure that runs after the resource has been closed, and the loan pattern that bounds *when* the resource is released cannot stop a reference from being smuggled out of the block. Scala 3's **capture checking** — the practical output of the Caprese research line and the *Capturing Types* paper (Boruch-Gruszecki, Odersky et al., ACM TOPLAS 2023) — annotates types with the set of capabilities a value may reference and rejects programs where that set outlives the capability's scope. The cost is that the capture set becomes part of the type: it propagates through every signature that touches a tracked value, it must be written or inferred at each boundary, and the feature remains experimental, with syntax and inference shifting between releases.

## Types that carry a capture set

A capability is a value the compiler treats as trackable. A **capturing type** is an ordinary type annotated with the set of capabilities it may reference, written `T^{c1, c2}`. The shorthand `T^` means `T^{cap}`: the type captures the **universal capability `cap`**, which stands for "possibly anything". An unannotated type such as `Int` has the empty capture set `{}` and is *pure*.

The rule that does the work is a propagation rule: **a value's capture set must include the capture sets of everything it closes over**. A closure that references a tracked file handle `f` therefore has type `(() => Unit)^{f}` rather than a plain `() => Unit`, and that set is carried outward through every expression the closure is part of.

Capture sets are ordered, and **capturing types come with a subtype relation in which a type with a smaller capture set is a subtype of the same type with a larger one**. A pure `() -> Unit` is thus usable where `(() => Unit)^{f}` is expected, but not the reverse. This is what makes the checker compositional: widening a capture set is always sound, narrowing it is never inferred by accident, and `cap` sits at the top of the ordering as the set that subsumes all others.

Function types acquire a second form under the feature. `A -> B` denotes a **pure** function — one whose capture set is empty — while `A => B` remains the ordinary function type and may capture. Code that must guarantee no capability is retained states that guarantee by writing the arrow, not by convention.

## The escaping-resource example

Consider the loan pattern. Without capture checking, the following compiles and fails at run time when the returned closure is invoked:

```scala
import language.experimental.captureChecking
import java.io.FileOutputStream

def usingLogFile[T](op: FileOutputStream^ => T): T =
  val logFile = FileOutputStream("log")
  val result = op(logFile)
  logFile.close()
  result
```

The `FileOutputStream^` annotation marks the parameter as a capability. An attempt to leak it looks like this:

```scala
val later = usingLogFile { f => () => f.write(0) }
later()   // would use a closed stream
```

The lambda returns `() => f.write(0)`, whose inferred type is `(() => Unit)^{f}`. The mechanism that rejects it runs in two steps. First, `f` is bound locally within `usingLogFile`, so **the name `f` cannot appear in the result type of the enclosing expression**; the checker widens the capture set `{f}` to the only set that remains available at that boundary, `{cap}`. Second, **a result may not capture the root capability `cap`**, and the widened type violates that rule. The compiler reports:

```
The expression's type () => Unit is not allowed to capture the root capability `cap`.
```

The diagnostic names `cap` rather than `f` because widening has already erased the local name — a point worth remembering when reading the error, since the offending capability is not the one mentioned. An `op` that consumes the file inside the block, returning a value whose capture set does not mention `f`, type-checks. The lifetime guarantee is static rather than a review convention.

## The `caps.Capability` model

Annotating every parameter with `^` is verbose. Extending `caps.Capability` marks a whole class as capability-carrying, so an implicit `{cap}` set is assumed and propagated without per-site annotation:

```scala
import language.experimental.captureChecking
import caps.Capability

class FileSystem extends Capability

class Logger(using FileSystem):
  def log(s: String): Unit = ???

def test(using fs: FileSystem) =
  val l: Logger^{fs} = Logger()   // l captures fs
  l.log("hello world!")
```

`Logger^{fs}` reads as "a logger that may use the filesystem capability `fs`". The capability is threaded through types rather than held in ambient global state, which is what allows the same machinery to serve as effect tracking: an effect is a capability that must be held in order to be performed, and the capture set of a value records which effects it can still perform.

### Implementation sketch (Scala)

A scoped connection handle, showing the load-bearing signatures rather than a working pool:

```scala
import language.experimental.captureChecking
import caps.Capability

class Connection extends Capability:
  def query(sql: String): List[String] = ???
  def close(): Unit = ???

/** `op` may use the connection; its result type mentions no capability,
  * so nothing derived from `c` can leave the block. */
def withConnection[T](open: () => Connection^)(op: Connection^ => T): T =
  val c = open()
  try op(c) finally c.close()

// Accepted: the result is a pure List[String].
val rows = withConnection(() => ???)(c => c.query("select 1"))

// Rejected: the returned thunk has type (() => List[String])^{c},
// widened to {cap} at the boundary and refused as a result.
// val deferred = withConnection(() => ???)(c => () => c.query("select 1"))

/** A pure arrow states that the transformation retains no capability. */
def render(rows: List[String]): String -> String =
  prefix => prefix + rows.mkString(",")
```

The only annotations carrying weight are `Connection^` on the parameter and the absence of any capture set on `T`. `T` is instantiated at the call site by inference; when inference would have to name `c` to describe the result, the widening step turns that into `cap` and the call fails to compile.

## Status and flags

Capture checking is **experimental**, and remains so in current Scala 3 releases. It is enabled per file with the language import shown above, or project-wide with the compiler flag:

```
-language:experimental.captureChecking
```

**Separation checking** is a further experimental layer in the same line, constraining tracked resources from being aliased into places that could use them concurrently or after release. Parts of the standard library have been annotated so that they interact correctly with the checker. Syntax and inference continue to change between releases, so a build that depends on the feature should pin its Scala version. Existing code is unaffected until the import or flag is added.

## Pitfalls

- **The error names `cap`, not the escaping value.** Because a local capability's name is widened out of the result type before the check runs, the diagnostic reports the root capability; searching the source for `cap` finds nothing, and the actual culprit is the local resource the closure retained.
- **`^` on a type alias or field silently widens.** Writing `T^` means `T^{cap}`, the largest capture set, so an annotation intended as "tracked" admits every capability and defeats the narrower `T^{c}` check the code was meant to enforce.
- **Returning a lazy value from a loan block fails even when the resource would still be open.** The checker reasons about scopes, not about the actual close time, so a `LazyList` or by-name result that mentions the capability is rejected regardless of whether it would in fact be forced before `close()`.
- **Pinning is required across minor releases.** Syntax and inference for the feature change between releases, so a project compiled with a floating Scala 3 version can stop compiling on an upgrade with no source change.
- **Extending `caps.Capability` changes inference for every use site of the class.** Instances acquire an implicit `{cap}` set that propagates into the types of everything closing over them, so annotating one class can surface capture errors in unrelated call sites that previously inferred plain types.
