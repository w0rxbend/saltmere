---
title: "Scala 3 to a native binary: GraalVM Native Image for fast startup"
date: 2026-07-26
track: scala-jvm
summary: "Why JVM cold start hurts scale-to-zero services, what GraalVM Native Image's closed-world AOT compilation buys you, and how to build a Scala 3 app into a native binary with sbt-native-image — including the reflection-config problem and honest before/after numbers."
reading_time: 5
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

A JVM microservice that handles one request per minute still pays for a full runtime: class loading, verification, and a cold JIT that hasn't compiled anything hot yet. On a small Scala 3 HTTP service that's several hundred milliseconds to first response and 150–300 MB of resident memory before you've served a byte. For a long-running server this amortizes to nothing. For a CLI, a Lambda, or a scale-to-zero container that spins up on traffic and dies, it's the whole cost.

GraalVM Native Image attacks this by moving the work from run time to build time. This post builds a Scala 3 app into a standalone native executable, deals with the reflection-configuration problem head-on, and puts real-shaped numbers next to the trade-offs.

## The closed-world bargain

`native-image` performs ahead-of-time (AOT) compilation under a **closed-world assumption**: it statically analyzes everything reachable from your `main`, compiles it to a native binary, and snapshots the initialized heap into the image. There is no bytecode interpreter and no JIT in the output — just machine code plus a minimal runtime ("Substrate VM") for GC and threads.

The payoff is direct: no class loading or verification at startup, no warmup, and a heap that begins pre-populated. Startup drops to milliseconds and RSS drops by 3–5x.

The catch is in the word *closed*. The analysis must see all reachable code at build time. Anything the JVM normally resolves dynamically — reflection, JNI, dynamic proxies, resource lookups by name, serialization — is invisible to static analysis and will fail at run time as a `ClassNotFoundException` or a missing-resource error *unless you declare it*. That declaration is the reflection-config problem, and it's the main thing that makes native builds fiddly.

The current release is **GraalVM 25, built on JDK 25** (the 25.x line shipped from September 2025; feature releases now land on a roughly monthly cadence — 25.1.x by mid-2026). GraalVM Community Edition is free and open source, which is what you want for a build tool in CI.

## Solving reflection config: the tracing agent + metadata repository

Two mechanisms carry the load so you rarely hand-write JSON:

1. **The tracing agent.** Run your app on a normal JVM with the agent attached; it records every reflective call, resource load, proxy, and JNI access that actually happens, and writes them out as metadata. You then feed that metadata to the build.

2. **The GraalVM Reachability Metadata repository.** A community-maintained store of pre-built configs for popular libraries. Frameworks and the native-build plugins pull matching metadata automatically, so you only trace the gaps your own code opens.

The raw agent invocation — attach it *before* `-jar` or the main class:

```shell
$JAVA_HOME/bin/java \
  -agentlib:native-image-agent=config-output-dir=src/main/resources/META-INF/native-image \
  -jar target/app.jar
```

On exit it writes metadata (`reflect-config.json`, `resource-config.json`, `jni-config.json`, `proxy-config.json`, `serialization-config.json`; recent GraalVM also emits a unified `reachability-metadata.json`). `native-image` automatically picks up anything under `META-INF/native-image/` on the classpath. Exercise real code paths while tracing — the agent only records what runs, so drive it with your actual endpoints or a test suite, and use `config-merge-dir` to accumulate across runs.

## Building a Scala 3 app with sbt-native-image

The `sbt-native-image` plugin wraps both the build and the agent so you stay in sbt.

`project/plugins.sbt`:

```scala
addSbtPlugin("org.scalameta" % "sbt-native-image" % "0.3.4")
```

`build.sbt` — a tiny service compiled with Scala 3:

```scala
lazy val svc = project
  .in(file("."))
  .enablePlugins(NativeImagePlugin)
  .settings(
    scalaVersion := "3.8.4",
    Compile / mainClass := Some("com.example.Main"),
    // Use a locally installed GraalVM 25 (e.g. via SDKMAN) rather than
    // the plugin's Coursier auto-download, which can lag on JDK versions.
    nativeImageInstalled := true,
    nativeImageOptions ++= List(
      "--no-fallback",              // fail the build instead of emitting a JVM-backed fallback
      "-O2",                        // optimize for runtime speed
      "--initialize-at-build-time"  // run static init at build time where safe
    )
  )
```

A minimal `Main` (no external deps needed to see the effect):

```scala
package com.example

@main def run(): Unit =
  val t = System.nanoTime()
  println(s"up in ${(System.nanoTime() - t) / 1000}us")
```

Build and run the binary:

```shell
sbt svc/nativeImage        # produces target/native-image/svc
./target/native-image/svc
```

If your code (or a library) uses reflection, generate the metadata with the plugin's agent wrapper, which runs the app on the JVM with the tracing agent and drops config into your resources so the next build consumes it:

```shell
sbt "svc/nativeImageRunAgent arg1 arg2"
sbt svc/nativeImage
```

## The honest trade-offs

Numbers below are *measured-style* from a small Scala 3 service on a warm laptop (JDK 25) — treat them as illustrative shape, not a benchmark. Your framework, heap, and machine will move them, but the order of magnitude is real and repeatable.

| Metric | JVM (`java -jar`) | Native Image |
|---|---|---|
| Time to first response | ~700–1200 ms | ~15–40 ms |
| Resident memory (RSS) | ~180–260 MB | ~30–55 MB |
| Binary/artifact | JAR + JVM install | single ~40–90 MB executable |
| Peak throughput (steady state) | higher (JIT warms up) | typically lower |
| Build time | seconds | **minutes** (whole-program analysis) |

What you give up is as important as what you gain:

- **No JIT, so no peak throughput.** For a service that runs for days under load, the JVM's profile-guided JIT eventually beats AOT. Native Image wins on *startup and footprint*, not sustained hot-loop throughput. (GraalVM's Profile-Guided Optimizations narrow this gap but add build steps.)
- **Build time and memory.** The closed-world analysis is expensive — expect minutes and gigabytes of build RAM. This belongs in CI, not your inner loop.
- **Reflection config is ongoing.** Every new library that reflects is a potential runtime failure you discover late. `--no-fallback` surfaces these at build time; the metadata repository and tracing agent keep the maintenance bounded but nonzero.

The decision rule is simple: reach for Native Image when a process is short-lived or scales to zero — CLIs, functions, batch jobs, sidecars — where startup and memory dominate the bill. Keep the plain JVM for long-lived, throughput-bound servers.

**Try next:** Take a real endpoint, run `sbt "svc/nativeImageRunAgent"` while hitting it with your test suite, diff the generated metadata under `META-INF/native-image/`, then rebuild with `--no-fallback` and confirm the binary serves the same request in single-digit milliseconds — and that removing one traced entry makes it fail, so you see exactly what the closed world needs.
