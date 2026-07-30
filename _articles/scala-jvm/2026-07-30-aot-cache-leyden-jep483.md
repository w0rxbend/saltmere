---
title: "The AOT cache: cutting JVM startup by ~40% with a training run (JEP 483)"
date: 2026-07-30
track: scala-jvm
summary: "Project Leyden's first shipped piece, JEP 483 in JDK 24, moves class loading and linking out of startup and into a pre-built cache. You do one training run, and every launch after skips the parse/load/link work. JDK 25 then collapsed the two-step process into one flag. Here's the workflow and the numbers."
reading_time: 5
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

The JVM's startup tax is real: every launch re-does the work of finding, reading, parsing, loading, and linking thousands of classes before your `main` runs. For a short-lived job, a CLI, or a serverless function, that work can dominate the useful runtime. Project Leyden's first shipped deliverable — **JEP 483, in JDK 24 (March 2025)** — attacks it directly: do that work *once*, ahead of time, and cache the result.

## The idea: shift work from every-run to a training-run

CDS (Class Data Sharing) has long let the JVM memory-map pre-parsed class metadata. JEP 483 goes further — it caches classes that are already **loaded and linked**, so a normal run skips not just parsing but the loading and linking steps too. The mechanism is a *training run*: you launch your app once in a representative way, the JVM records which classes get loaded and linked, and it bakes them into an **AOT cache** file. Every subsequent launch reads from that cache instead of redoing the work.

In JDK 24 this is a two-step process — record a configuration, then create the cache:

```bash
# Step 1: training run — record which classes load & link, into a config file.
java -XX:AOTMode=record -XX:AOTConfiguration=app.aotconf \
     -cp app.jar com.example.App

# Step 2: turn that config into the actual cache.
java -XX:AOTMode=create -XX:AOTConfiguration=app.aotconf \
     -XX:AOTCache=app.aot -cp app.jar

# Every real run: use the cache.
java -XX:AOTCache=app.aot -cp app.jar com.example.App
```

That's it — no source changes, no annotations, no framework buy-in. It works with any existing application, Scala included (the JVM doesn't care that your classes came from `scalac`).

## The numbers

The JEP cites two benchmarks against JDK 23:

- A small `HelloStream` program: **~42% faster startup** (0.031s → 0.018s), with an 11.4 MB cache.
- **Spring PetClinic 3.2.0**: **~42% faster** (4.486s → 2.604s), with a 130 MB cache.

Roughly 40% off cold startup, for free, is a big deal for anything you launch often and briefly — CLI tools, batch jobs, CI steps, scale-to-zero functions, and JVM-based dev loops.

## The one real constraint

The AOT cache is only valid if the real run is *essentially similar* to the training run — same JVM version, same classpath and module path. That makes sense: the cache encodes which classes existed and how they linked. Change the classpath and those assumptions break, so the JVM warns you and falls back rather than using a stale cache (unless you force it with `-XX:AOTMode=on`). Practically: **generate the cache as a build step, in an environment that matches production**, and treat it as a versioned artifact tied to your exact jar set. Don't train on your laptop and ship the cache to prod with a different classpath.

## JDK 25 made it one command

The two-step dance was friction, so **JDK 25 (September 2025, an LTS)** added **JEP 514 (Ahead-of-Time Command-Line Ergonomics)**, which collapses record-and-create into a single invocation:

```bash
# JDK 25: one step — train and produce the cache in one go.
java -XX:AOTCacheOutput=app.aot -cp app.jar com.example.App

# Use it exactly as before.
java -XX:AOTCache=app.aot -cp app.jar com.example.App
```

JDK 25 also shipped **JEP 515 (Ahead-of-Time Method Profiling)**, which stores *method execution profiles* in the same cache. That means the JIT compiler starts with warm profiling data from the training run, so hot methods get compiled sooner — attacking *warmup* (time-to-peak-throughput) on top of the *startup* win JEP 483 already delivered. Startup and warmup are the two halves of the "the JVM is slow to get going" complaint, and Leyden is now chipping at both.

## Where this is heading

JEP 483 is explicitly a *foundation*, not the destination — Leyden's roadmap layers more precomputation (AOT-compiled code, further linking) onto the same cache. For now, the pragmatic takeaway is small and concrete: if you run JVM processes that start often and don't live long, add an AOT-cache build step and take the ~40% startup cut. On JDK 25 it's a single extra command in your Dockerfile or CI pipeline, and the payoff shows up on every launch thereafter.

**Try next:** Take any Scala 3 app you run from the command line, build an AOT cache on JDK 25 with `-XX:AOTCacheOutput`, and `hyperfine` the startup time with and without `-XX:AOTCache` — then deliberately change the classpath and watch the JVM warn and fall back, which shows you exactly why the cache has to be a build-time artifact pinned to your jar set.
