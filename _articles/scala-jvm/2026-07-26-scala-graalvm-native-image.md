---
title: "Scala 3 to a native binary: GraalVM Native Image for fast startup"
date: 2026-07-26
track: scala-jvm
summary: "Why JVM cold start dominates the cost of scale-to-zero services, what GraalVM Native Image's closed-world ahead-of-time compilation buys, and how a Scala 3 application is built into a native binary with sbt-native-image — including the reflection-metadata problem and what the closed world costs."
reading_time: 7
tags: [scala, graalvm, native-image, jvm, aot, sbt]
sources:
  - title: "GraalVM Native Image — Collect Metadata with the Tracing Agent"
    url: "https://www.graalvm.org/latest/reference-manual/native-image/metadata/AutomaticMetadataCollection/"
  - title: "GraalVM Native Image — Reachability Metadata Repository"
    url: "https://www.graalvm.org/latest/reference-manual/native-image/metadata/"
  - title: "GraalVM Release Calendar"
    url: "https://www.graalvm.org/release-calendar/"
  - title: "scalameta/sbt-native-image"
    url: "https://github.com/scalameta/sbt-native-image"
  - title: "Scala 3 releases"
    url: "https://github.com/scala/scala3/releases"
---

**Gist.** A Java Virtual Machine (JVM) process pays a fixed entry cost on every start — class loading, bytecode verification, and a JIT compiler (one that translates bytecode to machine code during execution) that has not yet observed any hot method — which is amortized to nothing by a long-running server and paid in full by a process that lives for one request. GraalVM Native Image moves that work to build time: an ahead-of-time (AOT) compiler statically analyzes the program under a **closed-world assumption**, emits machine code, and snapshots an already-initialized heap into the executable. The cost of closing the world is that every dynamic lookup the JVM would have resolved at run time — reflection, Java Native Interface (JNI) calls, dynamic proxies, resources addressed by name, serialization — must be declared as metadata at build time or it fails in the produced binary.

## The closed-world bargain

`native-image` computes the set of methods, fields and classes reachable from the entry point, compiles that closure, and writes the resulting heap image into the executable. The output contains **no bytecode interpreter and no JIT**; it carries only machine code plus a minimal runtime, Substrate VM, providing garbage collection and threads.

The consequences follow mechanically from what has been removed. There is no class loading or verification at start, because classes exist only as compiled code and pre-initialized objects. There is no warmup, because there is no profile to collect and no tier to promote into. The heap begins populated rather than empty. The reported effect on small services is a startup time an order of magnitude lower and a materially smaller resident set size (RSS); no controlled benchmark is reproduced here, and the factor depends on framework, heap configuration and machine.

The same removal produces the failure mode. Static analysis can only follow edges it can see. A call such as `Class.forName(name)` where `name` is computed at run time has no edge in the call graph, so the class is not in the image, and the call raises `ClassNotFoundException` at run time rather than at build time. The same holds for a resource fetched by name, a `java.lang.reflect.Proxy` over an interface set, a JNI lookup, and serialization. **The declaration of these dynamic accesses is the reflection-configuration problem, and it is the main source of friction in native builds.**

GraalVM version numbers track the JDK they are built on, so **GraalVM 25 is the distribution built on JDK 25**. The release calendar publishes the dates for each line rather than a fixed monthly cadence, so the version to pin should be read from it at build time. GraalVM Community Edition is free and open source, which suits use as a build tool in continuous integration (CI).

## Supplying the metadata: tracing agent and reachability repository

Two mechanisms remove most of the hand-written JSON.

1. **The tracing agent.** The application runs on an ordinary JVM with the agent attached. The agent records each reflective call, resource load, proxy creation and JNI access **that executes**, and writes the record out as metadata consumed by the subsequent build. Its coverage is therefore exactly the coverage of the run: a code path not exercised under the agent produces no entry, and the corresponding failure appears later in the native binary.

2. **The GraalVM Reachability Metadata repository.** A community-maintained store of pre-built configurations for widely used libraries. Frameworks and the native-build plugins resolve matching metadata automatically, leaving only application-specific gaps to trace.

The raw agent invocation, with the agent attached **before** `-jar` or the main class:

```shell
$JAVA_HOME/bin/java \
  -agentlib:native-image-agent=config-output-dir=src/main/resources/META-INF/native-image \
  -jar target/app.jar
```

On exit the agent writes `reflect-config.json`, `resource-config.json`, `jni-config.json`, `proxy-config.json` and `serialization-config.json`; recent GraalVM releases also emit a unified `reachability-metadata.json`. `native-image` picks up anything found under `META-INF/native-image/` on the classpath without further flags. Because the agent observes only executed code, tracing runs should drive the real endpoints or the test suite, and `config-merge-dir` accumulates entries across several runs instead of overwriting them.

## Building a Scala 3 application with sbt-native-image

The `sbt-native-image` plugin wraps both the image build and the agent run as sbt tasks.

`project/plugins.sbt`:

```scala
addSbtPlugin("org.scalameta" % "sbt-native-image" % "0.3.4")
```

`build.sbt` for a small service compiled with Scala 3:

```scala
lazy val svc = project
  .in(file("."))
  .enablePlugins(NativeImagePlugin)
  .settings(
    scalaVersion := "3.3.4",
    Compile / mainClass := Some("com.example.Main"),
    // Build with the GraalVM already on the machine (JAVA_HOME) instead of
    // letting the plugin download one through Coursier.
    nativeImageInstalled := true,
    nativeImageOptions ++= List(
      "--no-fallback",                            // fail the build instead of emitting a JVM-backed fallback image
      "--initialize-at-build-time=com.example"    // scoped: only this package's static initializers run at build time
    )
  )
```

`--no-fallback` is the load-bearing flag. Without it, a build whose analysis detects unsupported dynamic access may emit a **fallback image**: an executable that requires a JVM at run time and therefore silently forfeits both the startup and the footprint gain. With it, the same condition ends the build.

A minimal entry point matching the `mainClass` above, with no external dependencies. The second branch is the one static analysis cannot follow: the argument is only known at run time, so nothing links `com.example.Handler` into the image unless metadata declares it.

```scala
package com.example

object Main:
  def main(args: Array[String]): Unit =
    args.toList match
      case Nil =>
        println("no handler requested")
      case name :: _ =>
        // Reachable only via a run-time string: absent from the call graph,
        // therefore absent from the image without reflection metadata.
        val cls = Class.forName(s"com.example.$name")
        val instance = cls.getDeclaredConstructor().newInstance()
        println(instance)
```

Building and running the binary:

```shell
sbt svc/nativeImage        # produces target/native-image/svc
./target/native-image/svc
```

Where application code or a dependency reflects, the plugin's agent wrapper runs the application on the JVM under the tracing agent and writes the configuration into the resource directory, so that the next image build consumes it:

```shell
sbt "svc/nativeImageRunAgent arg1 arg2"
sbt svc/nativeImage
```

## Trade-offs

The table states the direction of each effect rather than measured figures; no benchmark is reproduced here, and any absolute number depends on framework, heap configuration and machine.

| Metric | JVM (`java -jar`) | Native Image |
|---|---|---|
| Time to first response | class loading plus JIT warmup on every start | an order of magnitude lower |
| Resident memory (RSS) | higher (JVM plus its own metadata and code cache) | lower |
| Artifact | JAR plus a JVM installation | one self-contained executable |
| Peak throughput (steady state) | higher (JIT warms up) | typically lower |
| Build time | seconds | **minutes** (whole-program analysis) |

What is surrendered matters as much as what is gained.

- **No JIT, therefore no profile-guided peak.** For a process that runs for days under load, the JVM's profiling JIT compiler eventually outperforms AOT output. Native Image wins on *startup and footprint*, not on sustained hot-loop throughput. Profile-guided optimization, available in Oracle GraalVM rather than the Community Edition, narrows the gap at the cost of an extra instrumented build and a profiling run.
- **Build cost.** Whole-program analysis takes minutes and gigabytes of build-time memory, which places it in CI rather than in an edit-compile-run loop.
- **Metadata is an ongoing obligation.** Each newly introduced library that reflects is a potential run-time failure discovered late. `--no-fallback` converts part of that class of problem into a build failure; the metadata repository and the tracing agent bound the remaining maintenance without eliminating it.

The selection rule follows from which cost dominates: Native Image suits short-lived or scale-to-zero processes — command-line tools, functions, batch jobs, sidecars — where startup and memory dominate the cost; the plain JVM suits long-lived, throughput-bound servers.

## Pitfalls

- **The agent is attached after `-jar`.** The JVM treats the argument as an application argument rather than a virtual-machine option, no metadata is produced, and the config directory is silently empty.
- **A tracing run exercises only the happy path.** The entries for the error branch are missing, the build succeeds, and the binary fails with `ClassNotFoundException` the first time that branch is taken in production.
- **A second tracing run uses `config-output-dir` instead of `config-merge-dir`.** The earlier run's entries are overwritten rather than merged, and previously working reflective paths regress.
- **`--no-fallback` is omitted.** The build may emit a fallback image that still requires a JVM; startup and RSS stay at JVM levels while the pipeline reports a successful native build.
- **`--initialize-at-build-time` is applied to a class whose static initializer captures run-time state.** A value such as an open file descriptor, a hostname or a seeded random generator is frozen into the image heap and reused identically by every process started from the binary.
- **Build time is treated as a local-loop cost.** Whole-program analysis consumes minutes and gigabytes of RAM per build, so running it on every edit stalls development instead of the CI job.
