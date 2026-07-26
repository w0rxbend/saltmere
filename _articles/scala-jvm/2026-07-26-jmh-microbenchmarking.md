---
title: "JMH: Stop Benchmarking Your Optimizer, Not Your Code"
date: 2026-07-26
track: scala-jvm
summary: "Naive System.nanoTime() loops measure the JIT's cleverness, not your algorithm. JMH's @State, forks, and Blackhole fix that — here's the model and how to read its output honestly."
reading_time: 6
tags: [jmh, benchmarking, jvm, jit, performance, scala, sbt-jmh]
sources:
  - title: "openjdk/jmh (official repository)"
    url: "https://github.com/openjdk/jmh"
  - title: "JMHSample_08_DeadCode.java"
    url: "https://github.com/openjdk/jmh/blob/master/jmh-samples/src/main/java/org/openjdk/jmh/samples/JMHSample_08_DeadCode.java"
  - title: "Blackhole.java (jmh-core)"
    url: "https://github.com/openjdk/jmh/blob/master/jmh-core/src/main/java/org/openjdk/jmh/infra/Blackhole.java"
  - title: "Avoiding Benchmarking Pitfalls on the JVM (Oracle)"
    url: "https://www.oracle.com/technical-resources/articles/java/architect-benchmarking.html"
  - title: "sbt-jmh plugin"
    url: "https://github.com/sbt/sbt-jmh"
---

## The loop that lied

Every JVM developer eventually writes this:

```scala
val start = System.nanoTime()
var i = 0
while (i < 100000000) {
  compute(x)
  i += 1
}
println((System.nanoTime() - start) / 1e6 + " ms")
```

It compiles, it runs, it prints a number. The number is close to meaningless. The JVM is not a simple interpreter that does exactly what the bytecode says — it's an adaptive runtime that watches what you do and rewrites it. A "benchmark" like this one is really measuring the optimizer, not the code.

Four things go wrong, all documented by Aleksey Shipilëv and the JMH project itself:

- **JIT warmup.** The first thousands of calls run interpreted or through C1 (client) compilation. C2 (the optimizing compiler) only kicks in once a method is hot. If your loop finishes before that happens, you measured the interpreter, not the compiled steady state.
- **Dead-code elimination (DCE).** If `compute(x)`'s result is never used, the compiler is free to prove the call has no observable effect and delete it. JMH's own `JMHSample_08_DeadCode` demonstrates exactly this: a `measureWrong()` benchmark that discards its result runs suspiciously, impossibly fast, because there's no code left to run.
- **Constant folding.** If the compiler can prove an input never changes across the loop, it can precompute the result once and reuse it — you're now benchmarking a constant, not a computation.
- **Loop optimizations and on-stack replacement (OSR).** Long-running loops get compiled *while still executing* (OSR), and the compiler unrolls, hoists invariants, and merges iterations. A tight microbenchmark loop invites the compiler to notice invariants that a real call site, invoked from many places, never would.

None of this is a bug — it's the JIT doing its job. The bug is in the benchmark, which lets the compiler see conditions ("this input is constant," "this result is unused") that don't hold in production.

## The JMH model

JMH (Java Microbenchmark Harness) is the OpenJDK project — the same team that builds `javac` and HotSpot ships this tool specifically to fight the above. It runs each benchmark method in its own controlled harness: separate warmup and measurement phases, optional process forks, and explicit mechanisms to defeat DCE and constant folding.

The building blocks:

| Annotation / type | Purpose |
|---|---|
| `@Benchmark` | Marks a method as a measured workload |
| `@State(Scope.Thread\|Benchmark\|Group)` | Holds mutable fields the JIT can't treat as compile-time constants; scope controls sharing across threads |
| `@Setup` / `@TearDown` | Lifecycle hooks on a `@State` object, run outside the measured region |
| `@Warmup(iterations = n, time = t)` | Iterations discarded from results, run until the JIT stabilizes |
| `@Measurement(iterations = n, time = t)` | Iterations that count toward the reported score |
| `@Fork(n)` | Runs the benchmark in `n` fresh JVM processes, each with its own warmup, to cancel out JIT-profile and GC-history bias from one run |
| `@BenchmarkMode(Mode.Throughput\|AverageTime\|SampleTime\|SingleShotTime)` | What's measured: ops/time, time/op, latency distribution, or one-shot cost |
| `@Param({"1","10","100"})` | Sweeps a field over multiple values, generating one benchmark run per combination |
| `Blackhole` | An injected sink object whose `consume(...)` methods give the JIT a real, unpredictable side effect so it can't eliminate the computation |

`@State` matters as much as `Blackhole`. If your "input" is a `final` local or a literal, the compiler can constant-fold through it. Put it in a `@State`-annotated object instead — JMH allocates it in a way the compiler can't see across compilation units, which keeps it real.

## Naive vs. correct, side by side

Naive — looks reasonable, measures nothing useful:

```java
public class NaiveBenchmark {
    public static void main(String[] args) {
        long x = 41;
        long start = System.nanoTime();
        for (int i = 0; i < 1_000_000_000; i++) {
            fib(x); // result discarded -> DCE eats the whole loop
        }
        System.out.println((System.nanoTime() - start) / 1_000_000 + " ms");
    }
    static long fib(long n) { return n <= 1 ? n : fib(n - 1) + fib(n - 2); }
}
```

Correct, in JMH's model:

```java
@BenchmarkMode(Mode.AverageTime)
@OutputTimeUnit(TimeUnit.MICROSECONDS)
@State(Scope.Thread)
@Fork(value = 2, warmups = 1)
@Warmup(iterations = 5, time = 1)
@Measurement(iterations = 10, time = 1)
public class FibBenchmark {

    @Param({"10", "20", "30"})
    public long n;

    @Benchmark
    public void fib(Blackhole bh) {
        bh.consume(fib(n));
    }

    private static long fib(long n) {
        return n <= 1 ? n : fib(n - 1) + fib(n - 2);
    }
}
```

Here `n` lives in `@State`, so it isn't a compile-time constant; the result is fed to `Blackhole.consume`, so it can't be dead-code-eliminated; warmup iterations are discarded before measurement begins; and two forks mean the reported number isn't an artifact of one JVM's particular JIT decisions.

The same class works unchanged under **sbt-jmh** for Scala: add `addSbtPlugin("pl.project13.scala" % "sbt-jmh" % "<version>")` to `project/plugins.sbt`, `enablePlugins(JmhPlugin)` in `build.sbt`, put benchmark classes (Java or Scala) under the project, and run `Jmh/run -i 10 -wi 10 -f 2 -t 1 .*FibBenchmark.*`. sbt-jmh is a thin wrapper that generates the JMH harness classes at compile time via annotation processing, exactly like the Maven/Gradle plugins do for Java.

## Reading the output

A run reports something like:

```
Benchmark              (n)  Mode  Cnt   Score   Error  Units
FibBenchmark.fib        10  avgt   20   0.089 ± 0.003  us/op
FibBenchmark.fib        20  avgt   20  11.204 ± 0.415  us/op
FibBenchmark.fib        30  avgt   20  1382.7 ± 48.9   us/op
```

`Score` is the mean over all measurement iterations across all forks; `Error` is the half-width of the confidence interval (99.9% by default) — read it as "the true value is very likely `Score ± Error`," not as noise to ignore. If `Error` is a large fraction of `Score`, don't trust the ranking against a competing implementation until you add iterations or forks to tighten it. Two results whose `Score ± Error` ranges overlap are statistically indistinguishable — resist the urge to declare a winner anyway.

`Cnt` is the total number of measurement samples (iterations × forks). If someone hands you a benchmark with `Cnt: 1`, be suspicious — that's a single-shot run with no variance information at all.

## What JMH does not fix

JMH controls warmup and DCE; it does not control your algorithm's cache behavior, your machine's thermal throttling, background processes, or NUMA effects. Pin CPU affinity, disable turbo boost variance where possible, and treat a benchmark run on a laptop with a browser open as a rough estimate, not a verdict. And always sanity-check a suspiciously fast result against `JMHSample_08_DeadCode` and `JMHSample_09_Blackholes` in the official samples — if your number looks too good, it probably got eliminated, not executed.

**Try next:** write a two-benchmark JMH suite comparing `List.contains` vs. a `Set` lookup at three `@Param` sizes, and check whether your conclusion survives when you triple the fork count.
