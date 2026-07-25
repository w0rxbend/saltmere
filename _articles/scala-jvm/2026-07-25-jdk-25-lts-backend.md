---
title: "Free heap on the JDK 25 LTS: turning on compact object headers"
date: 2026-07-25
track: scala-jvm
summary: "JDK 25 (the September 2025 LTS) ships compact object headers as a real product feature. One flag shrinks every object's header from 12 bytes to 8, and on real services that means double-digit heap and GC savings for nothing."
reading_time: 5
tags: [jvm, jdk25, memory, gc, performance]
sources:
  - title: "JEP 519: Compact Object Headers"
    url: "https://openjdk.org/jeps/519"
  - title: "JDK 25 (OpenJDK project page, GA & JEP list)"
    url: "https://openjdk.org/projects/jdk/25/"
  - title: "Java 25 Integrates Compact Object Headers with JEP 519 — InfoQ"
    url: "https://www.infoq.com/news/2025/06/java-25-compact-object-headers/"
  - title: "Reduce Object Header Size and Save Memory in Java 25 — Baeldung"
    url: "https://www.baeldung.com/java-object-header-reduced-size-save-memory"
  - title: "Compact Object Headers: Reducing Java's Memory Footprint by 22% — Ionut Balosin"
    url: "https://ionutbalosin.com/2026/04/compact-object-headers-reducing-javas-memory-footprint-by-22/"
---

JDK 25 went GA on **16 September 2025** and is the LTS most vendors will support for years. Its virtual-threads-and-friends headline gets the attention, but the change most likely to show up on your bill is quieter: **JEP 519, Compact Object Headers**, graduated from experimental (JEP 450, JDK 24) to a **product feature** in 25. It costs you one flag and gives back heap.

## What actually shrinks

Every object on the HotSpot heap carries a header the JVM uses for locking, identity hash, GC state, and a pointer to its class. On 64-bit HotSpot with compressed class pointers that header is **96 bits — 12 bytes** (a 64-bit mark word plus a 32-bit compressed class word). JEP 519 packs the class information into the mark word so the whole header becomes **64 bits — 8 bytes**.

Four bytes sounds trivial until you remember it is *per object*. A service that keeps tens of millions of small objects live — think a cache of DTOs, a graph of domain entities, JSON nodes — is paying that tax on every one. Smaller headers also mean better alignment packing, so many objects shed an extra padding slot on top of the 4 bytes.

## Turning it on

It is off by default in JDK 25. Enable it with a single flag:

```bash
java -XX:+UseCompactObjectHeaders -jar app.jar
```

That is the whole change — no code edits, no recompile. Verify it took effect:

```bash
java -XX:+UseCompactObjectHeaders -Xlog:gc+init -version 2>&1 | grep -i "compact object headers"
```

A good way to *see* the win rather than trust it is [JOL](https://github.com/openjdk/jol) (Java Object Layout). Run the same class with and without the flag:

```bash
java -XX:+UseCompactObjectHeaders -cp jol-cli.jar org.openjdk.jol.Main internals java.lang.Object
# header: 8 bytes  ->  instead of 12 without the flag
```

## Is it worth it

The measured numbers are the reason to bother. On SPECjbb2015 the JEP and follow-up benchmarks report roughly **22% less heap usage and ~8% faster execution**; independent benchmarking (Balosin) lands in the same ~20% footprint-reduction range, and Amazon has cited **up to 30% CPU reduction** in production services thanks to fewer GC cycles and better cache behavior. Less live data means the collector runs less often and touches less memory each time — the second-order win usually beats the raw bytes saved.

The trade-off is small and bounded: worst-case throughput overhead is capped around 5%, and the class-pointer encoding constrains how many distinct classes can be loaded (an enormous number, but not infinite). For an ordinary backend service it is a safe, reversible experiment.

## The Scala angle

Scala programs are *especially* header-heavy: case classes, tuples, boxed primitives, and the many small immutable objects idiomatic Scala 3 allocates all pay the 12-byte tax. A functional pipeline that materialises millions of `case class` instances is close to the ideal workload for this flag, and nothing in your `build.sbt` or code has to change — it is a pure JVM launch option.

**Try next:** add `-XX:+UseCompactObjectHeaders` to one staging instance, leave the rest on stock JDK 25, and compare heap-after-GC and GC pause frequency for an hour under real traffic. If the graphs diverge in your favour, promote the flag — it is the cheapest performance win in the release.
