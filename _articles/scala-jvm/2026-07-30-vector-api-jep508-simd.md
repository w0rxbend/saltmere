---
title: "SIMD on the JVM: The Vector API and JEP 508"
date: 2026-07-30
track: scala-jvm
summary: "How Java's Vector API lowers ordinary float loops to hardware SIMD, why a scalar tail or a mask is required, and why the module is still incubating after ten rounds — pending Valhalla."
reading_time: 6
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

**Gist.** Commodity cores carry vector registers that apply one arithmetic instruction to many elements at once — single instruction, multiple data (SIMD) — but a scalar Java loop expresses one element per operation, and the HotSpot auto-vectorizer recovers the parallel form only for simple loop shapes and with no guarantee. The Vector API (`jdk.incubator.vector`) provides types whose operations the JIT compiler — HotSpot's runtime code generator — recognizes as intrinsics and lowers to the host's real vector instructions. The cost is that the loop shape becomes the programmer's responsibility: the register width is discovered at runtime, so every loop needs an explicit bound and an explicit treatment of the elements that do not fill a whole register.

## What the vector registers offer

An x64 core with AVX-512 has 512-bit vector registers, which hold **16 `float` lanes**. A single `vmulps` multiplies all 16 pairs. AArch64 exposes NEON at 128 bits, or the Scalable Vector Extension (SVE). A scalar loop leaves that width unused. The Vector API does not add hardware capability; it makes the intent explicit in bytecode so that lowering is not contingent on the auto-vectorizer's pattern matching.

## Species, lanes, masks

Three abstractions carry the model.

- **`VectorSpecies<E>`** pairs an element type with a register shape. `FloatVector.SPECIES_PREFERRED` resolves at runtime to the widest shape the executing hardware supports, so **one compiled class file uses 512-bit registers on an AVX-512 host and 128-bit registers on a NEON host** without recompilation.
- **Lanes** are the parallel slots of a species. `SPECIES.length()` returns how many elements advance per step — 16, 8, 4, and so on. This value is *not* a compile-time constant, which is the root of the loop-shape problem below.
- **`VectorMask<E>`** is a per-lane boolean predicate that gates which lanes participate in an operation. Masking is how a data-dependent branch is expressed without control flow, and how a partial final chunk is processed without leaving vector code.

## The loop-bound invariant

The invariant every vectorized loop must maintain is that **a full-width load starting at index `i` reads lanes `i` through `i + length() - 1`, all of which must be in bounds**. Since `length()` is unknown until runtime, the loop cannot be written against a fixed stride. `VectorSpecies.loopBound(n)` returns the largest multiple of `length()` not exceeding `n`, which is exactly the largest starting-index bound for which the invariant holds.

Fused multiply-add over float arrays — `out[i] = a[i]*b[i] + c[i]`, the kernel underneath dot products, filters and machine-learning primitives — shows the shape:

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

For an array of 1000 elements on a 16-lane species, `loopBound` returns 992; the vector loop covers indices 0–991 and the remaining 8 elements form the **scalar tail**. Array lengths rarely divide evenly by a runtime-determined register width, so a cleanup path is not optional.

The alternative to the scalar tail is a masked final step. `indexInRange(i, n)` produces a mask that is true for lanes below `n` and false above it; masked loads and stores treat the false lanes as inert, so **no read or write occurs past the end of the array**:

```java
for (; i < a.length; i += SP.length()) {
    var m = SP.indexInRange(i, a.length);          // VectorMask: true for valid lanes
    var va = FloatVector.fromArray(SP, a, i, m);
    var vb = FloatVector.fromArray(SP, b, i, m);
    var vc = FloatVector.fromArray(SP, c, i, m);
    va.fma(vb, vc).intoArray(out, i, m);           // out-of-range lanes are inert
}
```

This variant is uniform — one loop, no cleanup — at the price of carrying a mask through every iteration, including the full ones.

### Implementation sketch (Scala)

The module is a plain JVM module, so Scala 3 calls it directly. A masked reduction shows the same invariant from the other side: lanes outside the array must contribute the reduction's identity element, which `reduceLanes` with a mask supplies.

```scala
import jdk.incubator.vector.{FloatVector, VectorOperators, VectorSpecies}

object Dot:
  private val SP: VectorSpecies[java.lang.Float] = FloatVector.SPECIES_PREFERRED

  def dot(a: Array[Float], b: Array[Float]): Float =
    // accumulate lane-wise, then fold the accumulator once at the end
    var acc = FloatVector.zero(SP)
    var i = 0
    val bound = SP.loopBound(a.length)
    while i < bound do
      val va = FloatVector.fromArray(SP, a, i)
      val vb = FloatVector.fromArray(SP, b, i)
      acc = va.fma(vb, acc)
      i += SP.length()

    if i < a.length then
      val m = SP.indexInRange(i, a.length)
      val va = FloatVector.fromArray(SP, a, i, m)
      val vb = FloatVector.fromArray(SP, b, i, m)
      acc = va.fma(vb, acc)   // masked loads zero inactive lanes, so acc passes through

    acc.reduceLanes(VectorOperators.ADD)
```

Two details are load-bearing. **The accumulator is a vector, not a scalar** — folding once at the end keeps the loop free of a cross-lane reduction, which is the expensive operation. And **lane-wise accumulation reassociates the sum**, so the floating-point result differs from a left-to-right scalar loop; the difference is a consequence of non-associative floating-point addition, not a defect.

## Compiling and running

The module is an incubator, so it must be requested at both compile and run time:

```
javac --add-modules jdk.incubator.vector Fma.java
java  --add-modules jdk.incubator.vector Fma
```

Omitting the flag at compile time leaves the package unresolvable, so compilation fails; supplying it produces a warning that the module is incubating. Incubator modules are not stable across releases and the API may change between JDKs.

## Lowering

At runtime the JIT recognizes `FloatVector.fma` and the load/store idioms as intrinsics and emits native SIMD: `vfmadd*ps` on AVX-2 and AVX-512, the NEON or SVE equivalents on AArch64. A species wider than the host's registers is not rejected; the implementation emulates it, at a cost in throughput — which is why `SPECIES_PREFERRED` is the portable choice. The portability claim is therefore **one source form against `SPECIES_PREFERRED`, lowered to whatever instruction the host provides**.

## Incubation status

JEP 508 is the **Tenth Incubator**, delivered in **JDK 25**, the September 2025 long-term-support (LTS) release. The lineage is unbroken from JEP 338, the First Incubator in JDK 16, at roughly one round per release.

The JEP ties graduation to **Project Valhalla**. Two shapes in the current API are consequences of what Java generics express today. First, the element type appears as a boxed type parameter with hand-written `FloatVector` and `IntVector` subclasses, because a generic type cannot be parameterized over `float`. Second, vector instances have no meaningful identity, a property that **value classes** (JEP 401) make expressible. Both are the subject of Valhalla work, and the API is expected to graduate after those features rather than before.

## Pitfalls

- **Hard-coding a lane count.** `SPECIES_PREFERRED.length()` is a runtime value; a loop written with a literal stride of 8 reads out of bounds on a 16-lane host or wastes half the register on a 4-lane one. Symptom: `ArrayIndexOutOfBoundsException` or silent under-utilization that varies by machine.
- **Using `loopBound` and then forgetting the remainder.** `loopBound` rounds *down*. Without a scalar tail or a masked step, the final `a.length % length()` elements of `out` are never written and retain whatever they held before. Symptom: correct results for lengths that happen to be multiples of the lane count, wrong tails otherwise.
- **Expecting bit-identical results against a scalar loop.** Lane-wise accumulation changes the summation order, and floating-point addition is not associative. Symptom: a unit test comparing a vectorized reduction to a scalar one with exact equality fails on some inputs.
- **Comparing an un-warmed vector loop to a scalar one.** The operations are intrinsics only once the JIT has compiled the method; interpreted execution runs them as ordinary object allocations and calls. Symptom: the vector version measures slower than scalar in a microbenchmark without warm-up.
- **Shipping without `--add-modules jdk.incubator.vector` on the runtime command.** The flag is required separately at compile and run time. Symptom: code that compiles in the build but fails to resolve the module when launched.
- **Assuming the API is stable across JDK upgrades.** Incubator modules carry no compatibility guarantee between releases, so a JDK bump can break compilation of code that used the previous round's signatures.
