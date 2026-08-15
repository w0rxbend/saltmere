---
title: "JMH: Measuring the Code Rather Than the Optimizer"
date: 2026-07-26
track: scala-jvm
summary: "Naive System.nanoTime() loops measure the JIT compiler's cleverness rather than the algorithm. JMH's @State, forks and Blackhole constrain the optimizer; this is the model and how to read its output honestly."
reading_time: 7
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

**Gist.** A hand-written timing loop on the Java Virtual Machine (JVM) measures what the JIT compiler — HotSpot's dynamic, run-time compiler — decided to do with that loop, not what the code under test costs at a real call site: warmup, dead-code elimination and constant folding all apply to the benchmark and not to production. The Java Microbenchmark Harness (JMH) constrains the optimizer by separating warmup from measurement, running fresh forked JVM processes, holding inputs in objects the compiler cannot fold away, and sinking results into a `Blackhole`. The cost is that each measurement becomes far more expensive — multiple forks, each with discarded warmup iterations — and the reported score arrives with a confidence interval that frequently refuses to separate two candidates.

## The loop that lies

The canonical naive form:

```scala
val start = System.nanoTime()
var i = 0
while (i < 100000000) {
  compute(x)
  i += 1
}
println((System.nanoTime() - start) / 1e6 + " ms")
```

It compiles, runs, and prints a number close to meaningless. The JVM is an adaptive runtime that observes execution and rewrites it. Four distinct mechanisms, documented by the JMH project and by Oracle's benchmarking-pitfalls article, corrupt the measurement:

- **JIT warmup.** Early invocations run interpreted or through the C1 (client) compiler. C2, the optimizing compiler, engages only once a method is judged hot. **A loop that finishes before C2 compilation measures the interpreter and C1, not the steady state a long-running server would exhibit.**
- **Dead-code elimination (DCE).** When the result of `compute(x)` is never observed, the compiler may prove the call has no observable effect and remove it. JMH's `JMHSample_08_DeadCode` demonstrates exactly this: a `measureWrong()` benchmark that discards its result reports an impossibly small time, **because no code remains to execute**.
- **Constant folding.** If the input provably never changes across iterations, the computation can be evaluated once and the result reused. **The benchmark then measures the cost of reading a constant.**
- **Loop optimizations and on-stack replacement (OSR).** Long-running loops are compiled while still executing (OSR); the compiler unrolls them, hoists loop-invariant expressions and merges iterations. **A tight microbenchmark loop exposes invariants that a method invoked from many unrelated call sites never presents.**

None of these is a defect in the JVM. The defect is in the benchmark, which shows the compiler conditions — this input is constant, this result is unused — that do not hold at the production call site.

## The JMH model

JMH is developed under OpenJDK, in the same repository family as the other OpenJDK code tools. Each benchmark method runs inside a generated harness with distinct warmup and measurement phases, optional process forks, and explicit mechanisms against DCE and constant folding.

| Annotation / type | Purpose |
|---|---|
| `@Benchmark` | Marks a method as a measured workload |
| `@State(Scope.Thread\|Benchmark\|Group)` | Holds mutable fields; scope controls sharing across threads |
| `@Setup` / `@TearDown` | Lifecycle hooks on a `@State` object, run outside the measured region |
| `@Warmup(iterations = n, time = t)` | Iterations discarded from the reported result |
| `@Measurement(iterations = n, time = t)` | Iterations that contribute to the score |
| `@Fork(n)` | Runs the benchmark in `n` fresh JVM processes, each with its own warmup |
| `@BenchmarkMode(Mode.Throughput\|AverageTime\|SampleTime\|SingleShotTime)` | Operations per unit time, time per operation, a latency distribution, or one-shot cost |
| `@Param({"1","10","100"})` | Sweeps a field over several values, producing one run per combination |
| `Blackhole` | An injected sink whose `consume(...)` methods give the computation a consumer the compiler cannot prove is dead |

Two of these carry most of the weight. **`Blackhole.consume` defeats DCE** by making the produced value observable. **`@State` defeats constant folding**: a value held in a field of a state object is not a compile-time constant of the benchmark method, whereas a literal or an effectively final local can be folded through.

**`@Fork` addresses a different failure mode entirely.** A single JVM accumulates a profile — branch statistics, receiver-type profiles for virtual calls, class-loading order, garbage-collector history — and that profile is shaped by everything the process ran earlier, including other benchmarks in the same suite. Running the same benchmark in several fresh processes exposes whether the score depends on one process's particular set of compilation decisions. When per-fork scores disagree widely, the reported error widens accordingly, which is the intended signal.

### Naive versus harnessed

Naive, and measuring nothing useful:

```java
public class NaiveBenchmark {
    public static void main(String[] args) {
        long x = 20;
        long start = System.nanoTime();
        for (int i = 0; i < 1_000_000; i++) {
            fib(x); // result discarded -> DCE removes the loop body
        }
        System.out.println((System.nanoTime() - start) / 1_000_000 + " ms");
    }
    static long fib(long n) { return n <= 1 ? n : fib(n - 1) + fib(n - 2); }
}
```

Under JMH's model, `n` lives in a `@State` object so it is not a compile-time constant; the result reaches `Blackhole.consume` so it cannot be eliminated; warmup iterations are discarded before measurement starts; and multiple forks prevent the number from being an artifact of one JVM's compilation history.

### Implementation sketch (Scala)

sbt-jmh compiles Scala benchmark classes and generates the JMH harness at compile time through JMH's annotation processor, the same route JMH takes for Java sources. The state class must be a public, non-final class with a public no-argument constructor and non-private mutable fields: the generated harness subclasses it and assigns those fields directly.

```scala
@BenchmarkMode(Array(Mode.AverageTime))
@OutputTimeUnit(TimeUnit.MICROSECONDS)
@State(Scope.Thread)
@Fork(value = 2, warmups = 1)
@Warmup(iterations = 5, time = 1)
@Measurement(iterations = 10, time = 1)
class FibBenchmark:

  // var, not val: the generated harness assigns this field per @Param combination
  @Param(Array("10", "20", "30"))
  var n: Long = 0L

  @Benchmark
  def fib(bh: Blackhole): Unit =
    bh.consume(FibBenchmark.fib(n))

object FibBenchmark:
  def fib(n: Long): Long =
    if n <= 1 then n else fib(n - 1) + fib(n - 2)
```

Note that Scala annotation arguments that are arrays require `Array(...)` where Java uses brace syntax. Enabling the plugin requires `addSbtPlugin("pl.project13.scala" % "sbt-jmh" % "<version>")` in `project/plugins.sbt` and `enablePlugins(JmhPlugin)` in `build.sbt`; a run is invoked as `Jmh/run -i 10 -wi 10 -f 2 -t 1 .*FibBenchmark.*`.

## Reading the output

A run reports rows of the following shape; the scores below are illustrative placeholders, not a published measurement:

```
Benchmark              (n)  Mode  Cnt   Score   Error  Units
FibBenchmark.fib        10  avgt   20   0.089 ± 0.003  us/op
FibBenchmark.fib        20  avgt   20  11.204 ± 0.415  us/op
FibBenchmark.fib        30  avgt   20  1382.7 ± 48.9   us/op
```

`Score` is the mean over all measurement iterations across all forks. **`Error` is the half-width of the confidence interval — 99.9% by default — so the row asserts that the true value very likely lies in `Score ± Error`.** It is not noise to be discarded. **Two results whose `Score ± Error` intervals overlap are not distinguished by the experiment**, and no ranking between them is supported until iterations or forks are added to narrow the intervals.

`Cnt` is the number of measurement data points behind the score; in the averaging modes that is measurement iterations multiplied by forks. **A row with `Cnt: 1` carries no variance information at all** and cannot support a comparison.

## What the harness does not control

JMH governs warmup, DCE and constant folding within the process. It does not govern cache behaviour of the algorithm, thermal throttling of the host, competing processes, or non-uniform memory access (NUMA) effects on multi-socket machines. A run performed on a laptop with other applications active is an estimate rather than a verdict. A result that appears implausibly fast should be checked against `JMHSample_08_DeadCode` and `JMHSample_09_Blackholes` in the official samples before it is believed.

## Pitfalls

- **A benchmark that discards its result reports a time far below any plausible cost of the computation.** The optimizer removed the call; the fix is `Blackhole.consume`, not more iterations.
- **A benchmark whose input is a literal or an effectively final local reports a constant-time score independent of input size.** The value was folded at compile time; the input belongs in a `@State` field.
- **Scores that shift between runs of the same code at a single fork leave fork-dependent compilation decisions unmeasured.** A single fork bakes in one JVM's accumulated profile; raising `@Fork` exposes the spread instead of hiding it.
- **Declaring a winner from two rows whose `Score ± Error` intervals overlap is unsupported by the data**, regardless of how far apart the means are.
- **Benchmarking with `Cnt: 1`** yields a number with no error estimate, since a single sample admits no variance calculation.
- **In Scala, declaring a `@Param` or state field as `val`, or making the benchmark class `final` or an `object`,** conflicts with the generated harness, which requires an instantiable class with assignable fields.
- **Reporting only the steady-state score for code that runs briefly in production** answers a question the deployment never asks; warmup is discarded by design, so short-lived workloads need `SingleShotTime` rather than `AverageTime`.
