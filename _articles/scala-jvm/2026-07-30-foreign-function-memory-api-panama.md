---
title: "The Foreign Function & Memory API: calling C without JNI"
date: 2026-07-30
track: scala-jvm
summary: "JEP 454 finalized the Foreign Function & Memory API in JDK 22, retiring JNI boilerplate and the deprecated memory-access methods of sun.misc.Unsafe. Arena, MemorySegment, and a Linker-bound MethodHandle express a libc call in a dozen lines of Java or Scala."
reading_time: 6
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

**Gist.** Reaching native code from the Java Virtual Machine (JVM) historically required the Java Native Interface (JNI) — a generated header, a hand-written C shim compiled once per platform, and manual pin/release pairs whose misuse crashes the process — while off-heap memory required the internal class `sun.misc.Unsafe`. The **Foreign Function & Memory API** (FFM), finalized as [JEP 454](https://openjdk.org/jeps/454) in **JDK 22**, replaces both: a `Linker` binds a native symbol to a `MethodHandle`, and an `Arena` owns the lifetime of every `MemorySegment` allocated within it. The cost is that **each access is bounds- and liveness-checked**, that the C signature must be restated as a `FunctionDescriptor` the compiler cannot verify against the header, and that downcalls are a restricted operation gated behind a launcher flag.

## What FFM displaces

**JNI glue.** FFM invokes a native function through a `MethodHandle` obtained from `Linker.downcallHandle`. There is no C shim, no `javah`/`javac -h` step, and no per-platform native build: the binding is ordinary Java code.

**`sun.misc.Unsafe` memory access.** [JEP 471](https://openjdk.org/jeps/471) deprecated the **79 memory-access methods** of `Unsafe` for removal in JDK 23, with warnings on use and eventual removal deferred to later releases. The nominated replacements are `VarHandle` for on-heap access and FFM's `MemorySegment` for off-heap access. Code built on `Unsafe.allocateMemory` and `Unsafe.putLong` migrates onto `MemorySegment`, which adds the bounds and lifetime checks `Unsafe` lacks.

Two long-term-support (LTS) releases bracket the transition. FFM is a **preview API in JDK 21**, the preceding LTS, and **final from JDK 22 onward**, including JDK 25. On JDK 21 the compiler and runtime require `--enable-preview`; from JDK 22 they do not.

## The core abstractions

Four types in `java.lang.foreign` carry the work.

- **`Arena`** controls the lifetime of native memory. The Javadoc describes it as controlling "the lifecycle of native memory segments, providing both flexible allocation and timely deallocation." `Arena.ofConfined()` yields a scope usable with try-with-resources; on close, every segment allocated from it is **freed and invalidated**, so a subsequent access throws rather than reading reclaimed memory.
- **`MemorySegment`** is a bounds-checked view over a contiguous region, on- or off-heap. It occupies the role of the raw pointer, with the bound carried alongside the address.
- **`Linker`** and **`FunctionDescriptor`** describe a C function's signature and produce a **`MethodHandle`**; `SymbolLookup` resolves the symbol to an address.
- **`jextract`** mechanically generates Java bindings from native library headers. Hand-written linker code of the kind shown below suits one-off calls; a library of any size (SQLite, libgit2, OpenSSL) is bound once with `jextract` and the generated class imported.

The invariant that distinguishes FFM from `Unsafe` is **temporal, not spatial**: a segment records the arena that produced it, and every access first checks that this arena is still alive. A confined arena additionally records its owning thread, so access from another thread is rejected. The failure mode of `Unsafe` — a use-after-free that reads or writes an unrelated allocation and manifests as corruption at an unrelated point in the program — becomes a thrown exception at the point of the offending access.

## Calling `strlen` from libc

`strlen` has the C signature `size_t strlen(const char *s)`: one pointer in, an integer out.

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

The program runs directly as `java Strlen.java`. Three pieces carry the semantics. `FunctionDescriptor.of(returnLayout, argLayouts...)` mirrors the C prototype in the same order; `downcallHandle` turns that description into a callable handle; and the arena guarantees that the pointed-to string **outlives the call and is reclaimed on leaving the block**. No explicit `free` appears, and `cString` is unreachable in a usable state after the arena closes.

A nullary function is shorter: `getpid` needs `FunctionDescriptor.of(JAVA_INT)` and an argument-free `invoke`.

### Implementation sketch (Scala)

The API consists of ordinary Java classes, so Scala calls it unchanged. The one wrinkle is that `MethodHandle.invoke` is **signature-polymorphic**: its declared return type is erased, so the result requires an explicit cast.

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
  val len = strlen.invoke(s).asInstanceOf[Long]   // signature-polymorphic: erased return
  println(len)   // 6
}
```

`Using.resource` stands in for try-with-resources and closes the arena deterministically on both the normal and the exceptional path. Descriptors, segments and layouts are identical to the Java form.

## JNI compared with FFM

| | JNI | FFM (`java.lang.foreign`) |
|---|---|---|
| Native glue | Hand-written C shim, compiled per platform | None — pure Java bindings |
| Off-heap memory | `Unsafe` / manual `malloc`/`free` | `Arena` + `MemorySegment`, bounds-checked |
| Lifetime safety | Manual; use-after-free crashes the JVM | Arena close invalidates segments; access throws |
| Generating bindings | `javah`, boilerplate | `jextract` from C headers |
| Status | Legacy, still supported | Final since JDK 22 (preview in 19–21) |

## The restricted-operation gate

FFM code compiles and runs on any JDK 22 or later without a preview flag, but *calling* native code is a **restricted operation**. Depending on the JDK version and on how the code is packaged, the runtime either prints a warning or refuses the call unless native access is granted explicitly: `--enable-native-access=ALL-UNNAMED` for code on the class path, or the module name for modular code. **Allocation through an `Arena` is unrestricted; obtaining the linker and calling out through it are what is gated.** The distinction matters for deployment, because a program can pass every allocation-only test and then fail at its first downcall in a differently launched environment.

## Pitfalls

- **A `FunctionDescriptor` that disagrees with the C header is not a compile error.** Nothing checks the descriptor against the real prototype; a mismatched layout produces a garbage return value or memory corruption at the native side rather than a diagnostic.
- **Accessing a segment after its arena closes throws.** This is the designed behaviour, not a bug, and it appears when a segment escapes the try-with-resources or `Using.resource` block — for example when it is stored in a field or captured by a lambda that runs later.
- **A confined arena rejects access from a thread other than its creator.** A segment allocated in `Arena.ofConfined()` and then touched from a thread pool worker fails on the liveness/ownership check.
- **`MethodHandle.invoke` is signature-polymorphic, so the return type is erased.** In Scala, omitting `asInstanceOf` or casting to the wrong primitive width yields a `ClassCastException` or a wrong value rather than a compile error.
- **A program that only allocates off-heap memory proves nothing about native-access permissions.** The `--enable-native-access` gate applies to the restricted methods that reach native code, not to allocation, so the failure surfaces on the first native call, potentially only in production launch scripts.
- **JDK 21 requires `--enable-preview`.** Code compiled against the preview form of FFM on JDK 21 is not binary-compatible with a later JDK's final API and must be recompiled.
