---
title: "SIMD on the JVM: The Vector API and JEP 508"
date: 2026-07-30
track: scala-jvm
summary: "How Java's Vector API turns ordinary float loops into hardware SIMD, why you write a scalar tail, and why it's still incubating after ten rounds — waiting on Valhalla."
reading_time: 5
tags: [java, jvm, simd, vector-api, performance, panama, valhalla]
sources:
  - title: "JEP 508: Vector API (Tenth Incubator)"
    url: "https://openjdk.org/jeps/508"
  - title: "JEP 338: Vector API (First Incubator)"
    url: "https://openjdk.org/jeps/338"
  - title: "jdk.incubator.vector — Java SE 25 API docs (Oracle)"
    url: "https://docs.oracle.com/en/java/javase/25/docs/api/jdk.incubator.vector/jdk/incubator/vector/package-summary.html"
  - title: "JEP 401: Value Classes and Objects (Project Valhalla)"
    url: "https://openjdk.org/jeps/401"
---

Your CPU has been able to multiply eight floats in one instruction since about 2011. Your Java loop multiplies them one at a time. The Vector API is how you close that gap — explicitly, portably, without dropping to JNI.

## What SIMD actually buys you

A modern x64 core with AVX-512 has 512-bit vector registers: 16 `float` lanes wide. One `vmulps` instruction multiplies all 16 pairs at once. AArch64 gives you NEON (128-bit) or SVE. A scalar loop leaves that silicon idle; the HotSpot auto-vectorizer sometimes finds it, but only for simple shapes and with no guarantee. The Vector API makes the intent explicit so the JIT *reliably* lowers it to the real instructions.

## The vocabulary: species, lanes, masks

Three types do the work:

- **`VectorSpecies<E>`** — the element type plus the register width. `FloatVector.SPECIES_PREFERRED` picks the widest shape the current hardware supports at runtime, so the same bytecode uses 512 bits on AVX-512 and 128 on NEON.
- **Lanes** — the parallel slots. `SPECIES.length()` tells you how many elements move per step (16, 8, 4…).
- **`VectorMask<E>`** — a per-lane boolean predicate. It gates which lanes participate, which is how you do branches, and how you can handle a ragged tail without falling back to scalar.

## A real vectorized loop

Fused multiply-add over float arrays — `out[i] = a[i]*b[i] + c[i]` — the bread and butter of dot products, filters, and ML kernels:

```java
import jdk.incubator.vector.FloatVector;
import jdk.incubator.vector.VectorSpecies;

public final class Fma {
    static final VectorSpecies<Float> SP = FloatVector.SPECIES_PREFERRED;

    static void fma(float[] a, float[] b, float[] c, float[] out) {
        int i = 0;
        int bound = SP.loopBound(a.length);        // largest multiple of SP.length()

        for (; i < bound; i += SP.length()) {
            var va = FloatVector.fromArray(SP, a, i);
            var vb = FloatVector.fromArray(SP, b, i);
            var vc = FloatVector.fromArray(SP, c, i);
            va.fma(vb, vc).intoArray(out, i);      // one SIMD FMA across all lanes
        }

        for (; i < a.length; i++) {                // scalar tail
            out[i] = Math.fma(a[i], b[i], c[i]);
        }
    }
}
```

Two things to notice. `loopBound(length)` rounds *down* to a multiple of the lane count — if the array is 1000 long and you have 16 lanes, the vector loop covers 992 and stops. The remaining 8 elements are the **scalar tail**: array lengths rarely divide evenly by the register width, so you always need a cleanup loop (or a masked final step).

If you'd rather stay vectorized to the last element, replace the tail with a masked store:

```java
for (; i < a.length; i += SP.length()) {
    var m = SP.indexInRange(i, a.length);          // VectorMask: true for valid lanes
    var va = FloatVector.fromArray(SP, a, i, m);
    var vb = FloatVector.fromArray(SP, b, i, m);
    var vc = FloatVector.fromArray(SP, c, i, m);
    va.fma(vb, vc).intoArray(out, i, m);           // out-of-range lanes are inert
}
```

The mask makes the last partial chunk safe — lanes past the end neither load nor store.

## Running it

The module is still an incubator, so you opt in explicitly at both compile and run time:

```
javac --add-modules jdk.incubator.vector Fma.java
java  --add-modules jdk.incubator.vector Fma
```

Skip the flag and you get an "incubating module" compile error. This is deliberate friction: incubator modules are not stable across releases, and the API can shift between JDKs.

## How it lowers

At runtime the JIT recognizes `FloatVector.fma` and the load/store idioms as intrinsics and emits native SIMD — `vfmadd*ps` on AVX-2/AVX-512, the NEON/SVE equivalents on AArch64. When a target lacks the width you asked for, operations degrade gracefully by splitting across narrower registers rather than crashing. That's the whole pitch: **write once against `SPECIES_PREFERRED`, get the best instruction the host offers.**

## Why it's *still* incubating

JEP 508 is the **Tenth Incubator**, delivered in **JDK 25** (the September 2025 LTS). The lineage runs unbroken from JEP 338 (First Incubator, JDK 16) through one round per release. Ten-plus rounds is not a sign of trouble; it's a deliberate hold.

The blocker is **Project Valhalla**. Today the API fakes primitive generics with boxes — `Vector<Integer>` with hand-written `FloatVector`/`IntVector` subclasses — because Java generics can't say `Vector<float>`. And vector instances have no meaningful identity; they *want* to be value objects the JIT can keep in registers. Both wishes are exactly what Valhalla's **value classes** (JEP 401) and generic specialization over primitives will grant. The team would rather incubate for years than freeze a knowingly-wrong API shape — graduation is expected to follow Valhalla, not precede it.

**Try next:** Benchmark the FMA kernel above against a plain scalar loop with JMH on your own hardware — print `FloatVector.SPECIES_PREFERRED.length()` first to see how many lanes you're actually getting.
