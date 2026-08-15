---
title: "Scala Native 0.5: Ahead-of-Time Scala to a Native Binary Without GraalVM"
date: 2026-08-15
track: scala-jvm
summary: "Scala Native compiles Scala to a standalone native binary through LLVM — no JVM, no bytecode, no closed-world analysis over a full JDK. Covers how it differs from GraalVM Native Image, C interop via @extern and CFuncPtr, and what the 0.5.x line added (multithreading, delimited continuations, a raised LLVM baseline), with a C-calling example. Current stable is 0.5.12 (May 2026)."
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

**Gist.** Java virtual machine (JVM) deployment pays a startup and resident-memory cost that short-lived processes cannot amortise, and calling C from the JVM requires a foreign-function bridge. Scala Native removes both by skipping bytecode entirely: the compiler plugin emits its own intermediate representation, lowers it to LLVM intermediate representation (LLVM IR), and hands the result to `clang`, so the C application binary interface (ABI) is the native calling convention rather than a bridge. The cost is that the platform is not a JVM — the Java standard library is a re-implemented subset, and no dependency exists unless it has been cross-published for Scala Native.

## Two ahead-of-time pipelines that share no stages

GraalVM Native Image consumes compiled **JVM bytecode**, performs a closed-world reachability analysis over it, and emits a binary that still embeds a cut-down runtime (Substrate VM) with a garbage collector and JDK semantics. **Scala Native** produces no bytecode at any point. The Scala compiler plugin emits Native Intermediate Representation (NIR), an optimizer runs over NIR, and the lowered LLVM IR goes to `clang`, producing an ordinary native object linked against a hand-ported subset of `java.*`.

| | GraalVM Native Image | Scala Native |
|---|---|---|
| Input | JVM bytecode (any JVM language) | Scala source → NIR (Scala only) |
| Backend | Graal compiler → Substrate VM | LLVM IR → `clang` |
| Standard library | full JDK | re-implemented subset of `java.*` |
| Existing JVM `.jar` dependencies | mostly work (with metadata) | only if cross-published for Native |
| C interop | via Panama / JNI | first-class `@extern` / `CFuncPtr` |
| Garbage collector | Substrate VM GC | Immix / Commix (or Boehm) |

Two consequences follow from the absence of a bytecode stage. First, **an arbitrary JVM library JAR cannot be linked**; the dependency must be cross-published for Scala Native (`%%%` in sbt) or it does not exist for the platform. Second, because `java.*` is a port rather than the JDK itself, **some `java.*` APIs are absent or approximate**, and their absence surfaces at link time rather than at compile time in the developer's own module. In exchange, C interop uses the native ABI directly instead of the Java Native Interface (JNI).

Both pipelines yield the shared headline of ahead-of-time compilation — process startup without JVM warm-up, and no JVM heap and metaspace resident alongside the application — but no published benchmark separates the two backends under a common workload. The constraint sets differ more sharply than the runtime characteristics, so dependency availability is usually the deciding input.

### Implementation sketch (Scala)

An external C library is declared as an `@extern` object annotated with `@link` for the library to link against; **each method body is the literal keyword `extern`**, which marks the symbol as resolved by the linker rather than compiled from Scala. Types map to C through `CInt`, `CString`, `CSize`, `Ptr[?]`; C function pointers are `CFuncPtrN`, convertible to and from Scala lambdas.

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
  println(clib.abs(-7))                       // 7 — computed by libc
  Zone:                                       // arena; frees on block exit
    val cstr: CString = toCString("scala native")
    println(clib.strlen(cstr).toInt)          // 12
```

`Zone { ... }` is a scoped allocation arena. `toCString` allocates the C string inside the zone, and **all allocations made in the zone are freed when the block exits**, so no manual `free` call appears. The lifetime is therefore lexical: a `CString` that outlives its `Zone` points at released memory, and nothing in the type system prevents that escape.

Passing a Scala function where C expects a callback is symmetric — `CFuncPtr1.fromScalaFunction(f)` in the general case, or in Scala 3 a bare lambda where the expected type is already a `CFuncPtr`:

```scala
val cb: CFuncPtr1[CString, Unit] = (s: CString) => println(fromCString(s))
```

The sbt plugin drives the LLVM stage, and one command produces the binary:

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

`clang`/LLVM must be present on the build machine: the toolchain is a build-time prerequisite, not a bundled component, and **the 0.5 line raised its LLVM baseline**, so a distribution's older `clang` may be rejected.

## What the 0.5.x line added

The current stable release is **Scala Native 0.5.12 (May 2026)**; the series opened with **0.5.0 on 11 April 2024**.

- **Multithreading.** 0.5 added `java.lang.Thread` support with object monitors backing `synchronized`, `@volatile`, working `java.util.concurrent` types and atomics, and multithreading-aware garbage collection across the collectors. Earlier releases had no `Thread` support, so a collector never had to stop or scan a second mutator; the 0.5 collectors must.
- **Delimited continuations.** A stack-copying continuations primitive in the runtime, with the design published at the ACM SIGPLAN International Conference on Managed Programming Languages and Runtimes (MPLR '24). It is the substrate under fiber-style concurrency and under the **experimental JVM-style virtual threads** exposed later in the 0.5 line, which the release notes mark as not production-ready.
- **Raised LLVM baseline**, preliminary 32-bit architecture support (for example ARMv7), and initial source-level debugging with approximated stack-trace line numbers.
- **Scala 3 support** tracking current Scala 3 releases, alongside Scala 2.12 and 2.13.

**0.5.0 broke binary compatibility with 0.4.x**, so every dependency required republication against 0.5. That makes cross-published availability, not language features, the gating question for adoption.

Scala Native fits workloads where startup latency, resident footprint, and direct C interop dominate — command-line tools, small daemons, embedded targets. Workloads that depend on the full JDK or a wide graph of JVM-only JARs remain better served by the JVM, or by GraalVM Native Image where JVM-library breadth must survive into a binary.

## Pitfalls

- **A JVM dependency that is not cross-published for Scala Native fails at link time, not at compile time.** A dependency declared with `%%` names the JVM artifact, which resolves and compiles; `%%%` is what asks for the Native-suffixed artifact, and the mismatch only becomes visible when the linker looks for native code that was never published.
- **A missing `java.*` API surfaces as an unresolved symbol during linking**, because the standard library is a re-implemented subset rather than the JDK, and the reference may sit inside a transitive dependency rather than in the project's own code.
- **A `CString` used after its enclosing `Zone` block exits reads freed memory.** The zone frees every allocation made inside it on exit, and the type of the pointer carries no lifetime information.
- **Upgrading from 0.4.x with unmodified dependency coordinates fails resolution or linking**, because 0.5.0 is binary-incompatible with 0.4.x and every artifact had to be republished.
- **Virtual threads are a preview, not a supported feature.** Code written against them can break across 0.5.x patch releases, and the release notes decline to call the support production-ready.
- **Debugging a release build yields imprecise line numbers.** Source-level debugging in 0.5.x provides approximated stack-trace line information, so a reported frame may not correspond exactly to the source statement.
