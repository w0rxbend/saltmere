---
title: "The AOT cache: cutting JVM startup with a training run (JEP 483)"
date: 2026-07-30
track: scala-jvm
summary: "Project Leyden's first shipped piece, JEP 483 in JDK 24, moves class loading and linking out of startup and into a pre-built cache. A single training run records the loaded-and-linked classes; every later launch reads them back. JDK 25 collapsed the two-step process into one flag. The workflow, the published numbers, and the validity constraint."
reading_time: 9
tags: [jvm, project-leyden, jep-483, aot, startup, jdk-25, scala]
sources:
  - title: "JEP 483: Ahead-of-Time Class Loading & Linking — OpenJDK"
    url: "https://openjdk.org/jeps/483"
  - title: "JEP 514: Ahead-of-Time Command-Line Ergonomics — OpenJDK"
    url: "https://openjdk.org/jeps/514"
  - title: "JEP 515: Ahead-of-Time Method Profiling — OpenJDK"
    url: "https://openjdk.org/jeps/515"
  - title: "Run Into the New Year with Java's Ahead-of-Time Cache Optimizations — Inside.java"
    url: "https://inside.java/2026/01/09/run-aot-cache/"
  - title: "Let's Take a Look at JEP 483 — Gunnar Morling"
    url: "https://www.morling.dev/blog/jep-483-aot-class-loading-linking/"
---

**Gist.** Every Java Virtual Machine (JVM) launch repeats the work of finding, reading, parsing, loading and linking thousands of classes before `main` executes, and for a short-lived process that work can dominate total runtime. **JEP 483 (Ahead-of-Time Class Loading & Linking), shipped in JDK 24**, performs that work once during a *training run* and stores the resulting loaded-and-linked classes in an **ahead-of-time (AOT) cache** that later launches map back in. The cost is a validity constraint: the cache is usable only when the production run is essentially similar to the training run — same JDK release, same hardware architecture and operating system, consistent class paths and module options — so the cache becomes a build artifact that must be regenerated whenever the jar set changes.

## What the cache holds that Class Data Sharing did not

Class Data Sharing (CDS) already allowed the JVM to memory-map **pre-parsed class metadata**, removing the parse step from startup. JEP 483 extends the archive to classes that are **already loaded and linked**. Loading resolves a class through its defining class loader and installs it in the JVM's internal structures; linking performs verification, preparation of static fields, and the resolution work the specification permits at that point. A run backed by an AOT cache therefore arrives with reading, parsing, loading and linking already done, where a CDS archive covered only reading and parsing.

The cache does not cover everything. JEP 483 states as a non-goal the caching of classes loaded by user-defined class loaders: only classes loaded from the class path, the module path and the JDK itself, by the JDK's built-in class loaders, can be cached.

The recording step is empirical, not static. The JVM does not analyse the program to predict which classes it will need; it **observes an actual execution** and records what that execution loaded and linked. A training run that exercises an unrepresentative code path produces a cache that omits the classes the production path needs, and those classes are then loaded conventionally at runtime. Coverage of the cache is a property of the training workload, not of the application.

## The JDK 24 two-step workflow

```bash
# Step 1: training run — record which classes load and link, into a config file.
java -XX:AOTMode=record -XX:AOTConfiguration=app.aotconf \
     -cp app.jar com.example.App

# Step 2: turn that configuration into the cache itself.
java -XX:AOTMode=create -XX:AOTConfiguration=app.aotconf \
     -XX:AOTCache=app.aot -cp app.jar

# Every production run: use the cache.
java -XX:AOTCache=app.aot -cp app.jar com.example.App
```

The mechanism requires **no source changes, no annotations and no framework support**. It operates on class files, so a Scala application is covered on the same terms as a Java one: the JVM does not distinguish classes emitted by `scalac` from any others.

## Published measurements

JEP 483 reports two benchmarks measured against JDK 23:

- A small `HelloStream` program, which loads almost 600 JDK classes: startup falls from **0.031 s to 0.018 s (an improvement of 42%)**, with an **11.4 MB** cache.
- **Spring PetClinic 3.2.0**, which loads and links about 21,000 classes at startup: startup falls from **4.486 s to 2.604 s (also 42%, which JEP 483 calls a coincidence)**, with a **130 MB** cache.

The JEP also separates the two contributions by rebuilding the cache with `-XX:-AOTClassLinking`, which retains reading and parsing but drops loading and linking. **The split differs sharply between the two cases.** HelloStream reaches 0.027 s without loading and linking, a cumulative improvement of 13% out of the eventual 42%, so most of its gain comes from the new work. PetClinic reaches 3.008 s without them, a cumulative 33% out of the same 42%, so most of *its* gain comes from the reading and parsing that CDS already performed in earlier releases. Attributing a server application's whole startup improvement to JEP 483's new capability overstates it.

Two data points do not establish a general figure, and both are startup measurements rather than throughput or steady-state latency. The **cache sizes are the visible cost**: 130 MB of additional artifact for the PetClinic case, which must be built, stored, shipped in the container image, and paged in at launch.

## The validity constraint and the fallback

The cache encodes which classes existed and how they linked. JEP 483 enumerates what must match: **the same JDK release, the same hardware architecture and operating system, and consistent class paths** — a later run may append extra class-path entries to the training class path, but otherwise the class paths must be identical, and they must contain only jar files, because the JVM cannot check directories for consistency efficiently. Module options (`--module-path`, `--add-modules` and the rest) must be identical, and several options including `--patch-module` and `--limit-modules` must not be used at all. JVMTI agents that rewrite class files through `ClassFileLoadHook`, or that call `AddToBootstrapClassLoaderSearch` or `AddToSystemClassLoaderSearch`, are likewise excluded. Two exceptions are documented: training and later runs **may use different garbage collectors and different main classes** — the latter is what makes a dedicated trainer class practical.

When a constraint is violated the JVM by default **issues a warning and ignores the cache** rather than using stale linkage. `-XX:AOTMode=on` turns that into an error and exit; `-XX:AOTMode=off` disables the cache; `-XX:AOTMode=auto` is the default.

The operational consequence is that the cache is **a build-time artifact pinned to an exact jar set**, generated in an environment matching production and versioned alongside it. A cache trained on a developer machine and shipped to a production deployment with a different class path is ineffective without being fatal: the process still starts, a warning appears in the launch output, and the startup improvement disappears. The failure mode is **loss of the benefit without loss of correctness**, which is precisely the mode that escapes notice when launch logs are not inspected.

## JDK 25: one command, plus method profiles

**JDK 25 (September 2025, a long-term-support release)** added **JEP 514 (Ahead-of-Time Command-Line Ergonomics)**, which collapses record and create into a single invocation:

```bash
# JDK 25: train and produce the cache in one command.
java -XX:AOTCacheOutput=app.aot -cp app.jar com.example.App

# Consumption is unchanged.
java -XX:AOTCache=app.aot -cp app.jar com.example.App
```

The single invocation splits internally into the same two sub-invocations, `AOTMode=record` then `AOTMode=create`, writing the configuration to a temporary file and deleting it afterwards. The two-flag form remains available; JEP 514 states as a non-goal the introduction of new AOT optimizations, so the ergonomics change but the artifact's semantics and validity constraint do not.

JEP 514 documents two reasons to keep using the explicit two-step form. **The one-step workflow needs twice the heap**: the cache-creating sub-invocation allocates its own heap of the same size as the training run's, so `-Xms4g -Xmx4g` implies 8 GB for the combined run. And separating the steps allows training on an instance that resembles the deployment target while creating the cache on a larger one. The environment variable `JDK_AOT_VM_OPTIONS` passes options to the cache-creation sub-invocation only, which recovers some of that flexibility within the one-step form.

JDK 25 also shipped **JEP 515 (Ahead-of-Time Method Profiling)**, which stores **method execution profiles** from the training run in the same cache. The JIT compiler (which compiles bytecode to machine code during execution) consequently begins with profiling data rather than accumulating it from zero, so methods that were hot during training reach compiled form earlier. This targets **warmup — time to peak throughput — which is a distinct quantity from startup**. JEP 483 shortens the interval before `main` runs; JEP 515 shortens the interval before the code running under `main` is compiled well. An application can benefit from one and not the other.

The dependence on the training run is stronger here than for class loading, but it is not a correctness dependence. JEP 515 states that cached profiles do not prevent additional profiling during production runs: the JVM continues to profile and optimize as it runs, fusing cached profiles, on-line profiling and JIT compilation. A profile recorded from an unrepresentative workload therefore points the JIT at the wrong methods first and the on-line profiling corrects it, which spends compilation effort rather than producing a wrong answer.

## Scope of the deliverable

JEP 483 is stated as a foundation rather than an endpoint: Project Leyden's plan layers further precomputation onto the same cache. What is shipped and measurable today is the class loading and linking shift (JDK 24) and the profile and ergonomics additions (JDK 25). Claims about AOT-compiled method bodies in the cache belong to future work, not to the behaviour of a JDK 25 runtime.

The concrete applicability is narrow and identifiable: **processes that start frequently and terminate quickly** — command-line tools, batch jobs, continuous-integration steps, scale-to-zero functions, and local development loops — where startup is a meaningful fraction of the process lifetime. For a long-running server, the startup saving amortises to nothing and JEP 515's warmup effect is the only part that continues to matter.

An empirical check requires no more than building a cache with `-XX:AOTCacheOutput` on JDK 25, timing the launch with and without `-XX:AOTCache` under a repeated-measurement tool such as `hyperfine`, and then deliberately altering the class path to observe the warning and the fallback path.

## Pitfalls

- **The cache is ignored without ceremony after a class-path change.** The process starts normally and produces correct results; only the warning in the launch output and the restored startup time reveal that the cache was rejected, because the recorded linkage no longer matches the jar set.
- **A directory on the class path disqualifies the cache.** JEP 483 supports only jar files in class paths, so a launch script that appends `-cp build/classes` violates the constraint even when the contents are identical.
- **`-XX:AOTMode=on` converts a degraded launch into a failed one.** It reports an error and exits on any violated constraint or missing cache; JEP 483 recommends it for diagnostics only, since an incompatible VM option added outside the deployment's control — a cloud provider attaching a JVMTI agent, for instance — then prevents launch.
- **A JDK upgrade invalidates every cache.** The cache requires the same JDK release, so a base-image bump that changes the JDK build leaves the caches in that image unusable until they are regenerated.
- **An unrepresentative training run yields a small cache and a small benefit.** Classes never reached during training are absent from the cache and are loaded and linked conventionally at runtime; the measured improvement is bounded by the fraction of startup work the training run performed.
- **Cache size scales with the recorded class set.** The PetClinic cache is 130 MB, which enters the container image and the deployment artifact; treating the cache as free ignores the storage and image-pull cost it adds.
- **A rich test framework used as the training run inflates the cache.** JEP 483 advises against it: classes loaded only by the test framework are recorded and enlarge the cache without being needed in production, and no filtering mechanism exists yet.
- **The one-step JDK 25 workflow doubles peak heap demand.** The cache-creation sub-invocation runs with a heap the same size as the training run's, so a memory-constrained build container that fits `-Xmx4g` may not fit the combined workflow.
- **Classes loaded by user-defined class loaders are never cached.** Only the JDK's built-in loaders reading from the class path, module path and JDK are covered, so an application that loads plugins through its own loader gains nothing for those classes.
- **Warmup and startup are conflated.** JEP 483 addresses only the interval before `main`; an application whose complaint is time-to-peak-throughput needs JEP 515's profiles, and the two JEPs ship in different releases.
