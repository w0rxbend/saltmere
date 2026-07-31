---
title: "Scala 3 Capture Checking: Keeping Capabilities From Escaping"
date: 2026-07-31
track: scala-jvm
summary: "Scala 3's experimental capture checking tracks which capabilities a value holds so the compiler can reject a file handle, logger, or effect that outlives its scope. Here's the model, a working escaping-resource example, and the exact flags to turn it on."
reading_time: 5
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

Most resource bugs in the JVM world are lifetime bugs: a `FileOutputStream`, DB connection, or logger gets captured by a closure that runs after the resource is closed. `try`/finally and loan patterns bound *when* the resource is released, but nothing stops you from smuggling a reference out of the block. Scala 3's **capture checking** — the practical output of the Caprese research line and the *Capturing Types* paper (Odersky et al., ACM TOPLAS 2023) — makes the type system track exactly that.

## The core idea: types that carry a capture set

A capability is just a value the compiler treats as trackable. A **capturing type** is a normal type annotated with the set of capabilities it may reference, written `T^{c1, c2}`. The shorthand `T^` means `T^{cap}` — captures the universal capability `cap`, i.e. "anything". An unannotated type like `Int` captures nothing (`{}`) and is pure.

The rule that does the work: a value's capture set must include the capture sets of everything it closes over. So a closure that references a tracked file handle `f` gets type `(() => Unit)^{f}`, not a plain `() => Unit`. That capture set then propagates outward, and the compiler can refuse to let it cross a boundary where `f` is no longer valid.

## The escaping-resource example

Take the classic loan pattern. Without capture checking, this compiles and blows up at runtime:

```scala
import language.experimental.captureChecking
import java.io.FileOutputStream

def usingLogFile[T](op: FileOutputStream^ => T): T =
  val logFile = FileOutputStream("log")
  val result = op(logFile)
  logFile.close()
  result
```

The `FileOutputStream^` annotation marks the parameter as a capability. Now watch what happens when someone tries to leak it:

```scala
val later = usingLogFile { f => () => f.write(0) }
later()   // would use a closed stream
```

The lambda returns `() => f.write(0)`, whose inferred type is `(() => Unit)^{f}`. That closure captures the local capability `f`, and `usingLogFile` would let it escape as its result. The compiler rejects it:

```
The expression's type () => Unit is not allowed to capture the root capability `cap`.
```

Pass an `op` that actually consumes the file inside the block and it type-checks. The lifetime guarantee is now static.

## The `caps.Capability` model

Annotating every parameter with `^` is noisy. Extending `caps.Capability` marks a whole class as capability-carrying, so an implicit `{cap}` set is assumed and propagated for you:

```scala
import caps.Capability

class FileSystem extends Capability

class Logger(using FileSystem):
  def log(s: String): Unit = ???

def test(using fs: FileSystem) =
  val l: Logger^{fs} = Logger()   // l captures fs
  l.log("hello world!")
```

Here `Logger^{fs}` reads as "a logger that may use the filesystem capability `fs`". The capability is threaded explicitly through types instead of hiding in ambient global state — which is the whole point for effect tracking: an effect is just a capability you must hold to perform it.

## Current status and flags

Capture checking is **experimental and moving fast**. On Scala 3.8.1 (3.8.0, released 22 January 2026, had a runtime regression — use 3.8.1) you enable it per-file with the language import above, or project-wide with the compiler flag:

```
-language:experimental.captureChecking
```

Useful debugging flags: `-Vprint:cc` prints inferred capturing types, and `-Ycc-debug` dumps implementation detail. Scala 3.8 also shipped **separation checking** as a further experimental layer, ensuring tracked resources aren't aliased into places that could use them concurrently or after release. The standard library has been progressively re-annotated so it interacts correctly with the checker, but expect syntax and inference to keep shifting between releases — pin your Scala version if you build on it.

None of this affects existing code unless you opt in. It's a research feature you can try today on real resource-management code, not a language default.

**Try next:** In a Scala 3.8.1 scratch project, add `-language:experimental.captureChecking`, write the `usingLogFile` example above, and confirm the escaping-closure version fails; then rewrite one of your own loan-pattern or connection-pool helpers to take a `^`-annotated capability and see what leaks the compiler flags.
