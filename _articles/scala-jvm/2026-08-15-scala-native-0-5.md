---
title: "Scala Native 0.5: Ahead-of-Time Scala to a Native Binary Without GraalVM"
date: 2026-08-15
track: scala-jvm
summary: "Scala Native compiles Scala straight to a standalone native binary through LLVM — no JVM, no bytecode, no closed-world analysis over a full JDK. This covers how it differs from GraalVM Native Image, C interop via @extern and CFuncPtr, and what the 0.5.x line added (multithreading, delimited continuations, LLVM 16+), with a working C-calling example. Current stable is 0.5.12 (May 2026)."
reading_time: 6
tags: [scala-native, aot, llvm, native, c-interop, scala-3]
sources:
  - title: "Scala Native — official documentation"
    url: "https://scala-native.org/en/stable/"
  - title: "Scala Native 0.5.0 changelog (2024-04-11)"
    url: "https://scala-native.org/en/stable/changelog/0.5.x/0.5.0.html"
  - title: "Release Scala Native 0.5.12 — GitHub"
    url: "https://github.com/scala-native/scala-native/releases/tag/v0.5.12"
  - title: "Native code interop (@extern, CFuncPtr, Zone) — Scala Native docs"
    url: "https://scala-native.org/en/stable/user/interop.html"
  - title: "Stack-Copying Delimited Continuations for Scala Native — ACM (MPLR '24)"
    url: "https://dl.acm.org/doi/10.1145/3679005.3685979"
---

There are two ways to turn Scala into a native executable, and they share almost nothing under the hood. GraalVM Native Image takes compiled JVM **bytecode**, runs a closed-world analysis over it, and emits a binary that still bundles a cut-down runtime (Substrate VM) with a real garbage collector and the full JDK semantics baked in. **Scala Native** never produces bytecode at all: the Scala compiler plugin emits its own intermediate representation (NIR), an optimizer works over that, and the result is lowered to **LLVM IR** and handed to `clang`. What comes out is an ordinary native object — the same pipeline a C++ program takes — linked against a re-implemented subset of the Java standard library written for the native world.

The practical upshot is the same headline as GraalVM (millisecond startup, tens of MB of RSS instead of hundreds), but the constraints are different, and knowing which tool fits which job matters.

## Not GraalVM: a separate toolchain and a partial stdlib

The single most important difference is that Scala Native is **not a JVM at all** and does not consume JVM bytecode. That has consequences in both directions.

| | GraalVM Native Image | Scala Native |
|---|---|---|
| Input | JVM bytecode (any JVM lang) | Scala source → NIR (Scala only) |
| Backend | Graal compiler → Substrate VM | LLVM IR → `clang` |
| Standard library | full JDK | re-implemented subset of `java.*` |
| Existing JVM `.jar` deps | mostly work (with metadata) | only if cross-published for Native |
| C interop | via Panama / JNI | first-class `@extern` / `CFuncPtr` |
| GC | full Substrate GC | Immix / Commix (or Boehm) |

Because there is no bytecode step, you cannot take an arbitrary JVM library JAR and link it: a dependency must be **cross-published for Scala Native** (`%%%` in sbt) or it does not exist for the platform. And because the standard library is a hand-ported subset rather than the real JDK, some `java.*` corners are missing or approximate. In exchange, Scala Native's C interop is not an afterthought bolted on with JNI — it is the native ABI directly, which is where it pulls ahead of the JVM options.

## Calling C is a first-class operation

You declare an external C library as an `@extern` object, annotate it with `@link` for the library to link against, and each method body is the literal keyword `extern`. Types map to C via `CInt`, `CString`, `CSize`, `Ptr[?]`, and friends; C function pointers are `CFuncPtrN`, convertible to and from Scala lambdas.

```scala
//> using platform scala-native
//> using scala 3.3.6

import scala.scalanative.unsafe.*

@link("c")          // link against libc
@extern
object clib:
  def abs(n: CInt): CInt = extern            // int abs(int)
  def strlen(s: CString): CSize = extern     // size_t strlen(const char*)

@main def run(): Unit =
  println(clib.abs(-7))                       // 7 — computed by libc, not the JVM
  Zone:                                       // arena; frees on block exit
    val cstr: CString = toCString("scala native")
    println(clib.strlen(cstr).toInt)          // 12
```

`Zone { ... }` is a scoped allocation arena: `toCString` allocates the C string inside it, and everything is freed when the block exits, so there is no manual `free`. Passing a Scala function where C wants a callback is symmetric — `CFuncPtr1.fromScalaFunction(f)` in the general case, or in Scala 3 a bare lambda where the expected type is already a `CFuncPtr`:

```scala
val cb: CFuncPtr1[CString, Unit] = (s: CString) => println(fromCString(s))
```

Wire it up with the sbt plugin and one command produces the binary:

```scala
// project/plugins.sbt
addSbtPlugin("org.scala-native" % "sbt-scala-native" % "0.5.12")

// build.sbt
enablePlugins(ScalaNativePlugin)
scalaVersion := "3.3.6"
nativeConfig ~= { c =>
  c.withMode(scalanative.build.Mode.releaseFast)   // optimize the binary
   .withLTO(scalanative.build.LTO.thin)
}
```

```shell
sbt run          # compiles via LLVM and runs the produced binary
# the executable lands under target/scala-3.x/<project>-out
```

You need `clang`/LLVM on the machine — **LLVM 16 or newer is now the recommendation** (older versions are deprecated but still accepted).

## What the 0.5.x line actually added

The current stable release is **Scala Native 0.5.12** (22 May 2026); the 0.5 series opened with 0.5.0 back on 11 April 2024. The line's headline features:

- **Multithreading.** This is the big one. 0.5 added real `java.lang.Thread` support with object monitors backing `synchronized`, `@volatile`, working `java.util.concurrent` types and atomics, and — critically — multithreading-aware GC across all collectors. Before 0.5, Scala Native was effectively single-threaded.
- **Delimited continuations.** A stack-copying continuations primitive in the runtime (the design was published at MPLR '24), the substrate that makes fiber-style concurrency and, in 0.5.12, **experimental JVM-style virtual threads** possible — though those are Unix-only and explicitly not production-ready yet.
- **Updated LLVM baseline** to 16+, plus preliminary 32-bit architecture support (e.g. ARMv7) and initial source-level debugging with approximated stack-trace line numbers.
- **Scala 3** is fully supported (through 3.8.x as of 0.5.12), alongside Scala 2.12 and 2.13.

The honest caveats: 0.5.0 broke binary compatibility with 0.4.x, so every dependency had to be republished — check that the libraries you need actually cross-publish for 0.5 before committing. The stdlib is a subset, so audit for missing `java.*` APIs early rather than at link time. And multithreading, while real, is younger than the JVM's — the virtual-thread support in particular is a preview, and LTO builds of it are known to fail. Reach for Scala Native when startup, footprint, and native C interop dominate — CLIs, small daemons, embedded targets — and stay on the JVM (or GraalVM, if you need JVM-library breadth in a binary) when you depend on the full JDK or a wide graph of JVM-only jars.

**Try next:** `sbt new scala-native/scala-native.g8`, drop the `@extern` `clib` object above into `Main.scala`, run `sbt run`, then `ls -la target/scala-3.*/*-out` and time the binary with `time ./…-out` — compare its startup against `time scala Main.scala` on the JVM to see the millisecond-vs-hundreds-of-ms gap for yourself.
