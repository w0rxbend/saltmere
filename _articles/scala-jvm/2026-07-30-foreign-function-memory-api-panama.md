---
title: "The Foreign Function & Memory API: calling C without JNI, finally stable"
date: 2026-07-30
track: scala-jvm
summary: "JEP 454 finalized the Foreign Function & Memory API in JDK 22, retiring JNI boilerplate and the soon-to-be-removed sun.misc.Unsafe. Arena, MemorySegment, and a Linker-bound MethodHandle let you call a libc function in a dozen lines — from Java or Scala."
reading_time: 5
tags: [jvm, panama, ffm, native-interop, scala, java22]
sources:
  - title: "JEP 454: Foreign Function & Memory API"
    url: "https://openjdk.org/jeps/454"
  - title: "java.lang.foreign — Java SE 22 & JDK 22 API Specification"
    url: "https://docs.oracle.com/en/java/javase/22/docs/api/java.base/java/lang/foreign/package-summary.html"
  - title: "JEP 471: Deprecate the Memory-Access Methods in sun.misc.Unsafe for Removal"
    url: "https://openjdk.org/jeps/471"
  - title: "JDK 22 in Two Minutes — Sip of Java, Inside.java"
    url: "https://inside.java/2024/03/21/sip095/"
  - title: "Guide to Java Project Panama — Baeldung"
    url: "https://www.baeldung.com/java-project-panama"
---

For 25 years, "call this C function from Java" meant JNI: a hand-written header, a companion `.c` shim compiled per platform, manual `GetPrimitiveArrayCritical`/`Release` pairs, and a JVM crash if you got the pin/release dance wrong. The escape hatch for off-heap memory was worse — `sun.misc.Unsafe`, an internal class that was never meant to be public and is now on death row. The **Foreign Function & Memory API** (FFM), finalized as [JEP 454](https://openjdk.org/jeps/454) in **JDK 22** (March 2024), replaces both. The JEP's own framing: it lets programs "call native libraries and process native data without the brittleness and danger of JNI."

## The two things it actually replaces

**JNI boilerplate.** FFM calls a native function directly through a `MethodHandle`. No C shim, no `javah`/`javac -h`, no per-platform build step — the binding is pure Java.

**`sun.misc.Unsafe`.** [JEP 471](https://openjdk.org/jeps/471) deprecated the 79 memory-access methods of `Unsafe` for removal, starting in JDK 23, and JDK 24 turns their use into runtime warnings. The named replacements are `VarHandle` (for on-heap) and FFM's `MemorySegment` (for off-heap). If you have code doing `Unsafe.allocateMemory`/`putLong`, FFM is the migration target, and it comes with bounds and lifetime checks `Unsafe` never had.

Two LTS releases now carry the relevant context: FFM is still a *preview* in JDK 21 (the previous LTS), and *final* everywhere from JDK 22 onward — including JDK 25, the current LTS. If you're on 21, you need `--enable-preview`; on 22+ you don't.

## The core abstractions

Four types do the work, all in `java.lang.foreign`:

- **`Arena`** controls the lifetime of native memory. The Javadoc: it "controls the lifecycle of native memory segments, providing both flexible allocation and timely deallocation." `Arena.ofConfined()` is a `try`-with-resources scope — when it closes, every segment it allocated is freed *and invalidated*, so a later access throws instead of corrupting the heap.
- **`MemorySegment`** is a bounds-checked view over a contiguous region of memory, on- or off-heap. This is the `Unsafe` pointer, made safe.
- **`Linker`** + **`FunctionDescriptor`** describe a C function's signature and hand you a **`MethodHandle`** to call it. `SymbolLookup` resolves the symbol address.
- **`jextract`** is the tool that reads C headers and generates these bindings for you — "a tool to mechanically derive Java bindings from a set of native headers." You write the hand-rolled linker code below only for one-off calls; for a real library (SQLite, libgit2, OpenSSL) you run `jextract` once and import the generated class.

## Calling `strlen` from libc

A complete, compilable JDK 22 program. `strlen` has the C signature `size_t strlen(const char *s)` — one pointer in, an integer out:

```java
import java.lang.foreign.*;
import java.lang.invoke.MethodHandle;
import static java.lang.foreign.ValueLayout.*;

public class Strlen {
    public static void main(String[] args) throws Throwable {
        Linker linker = Linker.nativeLinker();
        SymbolLookup libc = linker.defaultLookup();   // libc is on the default lookup

        // size_t strlen(const char *s)  ->  (ADDRESS) size_t, modeled as JAVA_LONG
        MethodHandle strlen = linker.downcallHandle(
            libc.find("strlen").orElseThrow(),
            FunctionDescriptor.of(JAVA_LONG, ADDRESS));

        try (Arena arena = Arena.ofConfined()) {
            // allocateFrom writes a NUL-terminated C string into off-heap memory
            MemorySegment cString = arena.allocateFrom("panama");
            long len = (long) strlen.invoke(cString);
            System.out.println(len);   // 6
        }   // arena closes here: cString's memory is freed and invalidated
    }
}
```

Run it with `java Strlen.java`. Note the shape: `FunctionDescriptor.of(returnLayout, argLayouts...)` mirrors the C prototype, `downcallHandle` compiles that into a callable handle, and the `Arena` guarantees the string we passed by pointer outlives the call and is reclaimed the instant we leave the block. There's no explicit `free` and no way to touch `cString` after the arena closes.

`getpid` is even shorter — no arguments, so `FunctionDescriptor.of(JAVA_INT)` and `getpid.invoke()` with an empty arena.

## Invoking it from Scala

The API is plain Java classes, so Scala calls it verbatim — the only wrinkle is `MethodHandle.invoke`, which is *signature-polymorphic*. Scala 3 supports these, but cast the erased return explicitly:

```scala
import java.lang.foreign.*, java.lang.foreign.ValueLayout.*
import java.lang.invoke.MethodHandle
import scala.util.Using

val linker = Linker.nativeLinker()
val strlen: MethodHandle = linker.downcallHandle(
  linker.defaultLookup().find("strlen").orElseThrow(),
  FunctionDescriptor.of(JAVA_LONG, ADDRESS))

Using.resource(Arena.ofConfined()) { arena =>
  val s   = arena.allocateFrom("panama")
  val len = strlen.invoke(s).asInstanceOf[Long]
  println(len)   // 6
}
```

`Using.resource` is the idiomatic stand-in for Java's try-with-resources, so the arena still closes deterministically. Everything else — descriptors, segments, layouts — is identical to the Java version.

## JNI vs. FFM, at a glance

| | JNI | FFM (`java.lang.foreign`) |
|---|---|---|
| Native glue | Hand-written C shim, compiled per platform | None — pure Java bindings |
| Off-heap memory | `Unsafe` / manual `malloc`/`free` | `Arena` + `MemorySegment`, bounds-checked |
| Lifetime safety | Manual; use-after-free crashes the JVM | Arena close invalidates segments; access throws |
| Generating bindings | `javah`, boilerplate | `jextract` from C headers |
| Status | Legacy, still supported | Final since JDK 22 (preview in 19–21) |

## The one runtime gotcha: `--enable-native-access`

FFM code compiles and runs on any JDK ≥ 22 with no preview flag. But *calling* native code is a restricted operation. Depending on the JDK and how your code is packaged, the runtime may print a warning — or, on newer JDKs tightening this down, refuse the call — unless you grant native access explicitly: `--enable-native-access=ALL-UNNAMED` for the classpath, or name your module. Pure off-heap `MemorySegment` allocation via `Arena` is unrestricted; it's the *downcalls* that are gated. Wire the flag into your launch scripts early so it doesn't surprise you in production.

**Try next:** save the `Strlen.java` snippet, run it with `java Strlen.java`, then change `strlen` to `getpid` — swap the descriptor to `FunctionDescriptor.of(JAVA_INT)`, drop the argument, and confirm the returned PID matches what your shell reports for that process.
