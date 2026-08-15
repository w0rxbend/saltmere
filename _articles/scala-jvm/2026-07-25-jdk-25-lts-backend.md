---
title: "Compact object headers on the JDK 25 LTS"
date: 2026-07-25
summary: "JDK 25, the September 2025 long-term-support release, promotes compact object headers to a product feature. One launch flag shrinks every heap object's header from 12 bytes to 8, with reported heap reductions near 22% on SPECjbb2015 and a documented worst-case throughput cost."
track: scala-jvm
reading_time: 6
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

**Gist.** Every object allocated on the HotSpot heap carries a fixed header that stores locking state, the identity hash code, garbage-collection (GC) metadata and a reference to the object's class; on 64-bit HotSpot with compressed class pointers that header occupies **96 bits (12 bytes)** regardless of how small the object's own fields are. **JEP 519, Compact Object Headers**, encodes the class reference inside the mark word so the header becomes **64 bits (8 bytes)**, and it graduated from experimental status in JDK 24 (JEP 450) to a product feature in JDK 25, the long-term-support (LTS) release that went general availability on **16 September 2025**. The cost is a narrower encoding for class identity — the number of distinct loaded classes the encoding can address is large but finite — plus a **worst-case throughput regression on some workloads**, which the JEP records without promoting it to a general bound.

## The header layout and what the encoding changes

A HotSpot object header on a 64-bit virtual machine (VM) with compressed class pointers enabled has two parts: a **64-bit mark word**, which holds the fields the runtime mutates during the object's lifetime (locking state, the identity hash once computed, GC age and marking bits), and a **32-bit compressed class word**, which identifies the object's class metadata. The two together are 96 bits, and they precede the instance fields of every object without exception — a `java.lang.Object` instance with no fields at all still pays them.

JEP 519 removes the separate class word. **The class information is packed into the mark word alongside the existing lock, hash and GC bits**, leaving a single 64-bit header. The saving is therefore **4 bytes per object**, and it is per *object*, not per class or per allocation site. A service holding tens of millions of small live objects — a cache of data-transfer objects, a graph of domain entities, a parsed JavaScript Object Notation (JSON) document tree — pays the difference on every one of them.

The saving is frequently larger than 4 bytes. HotSpot rounds each instance's total size up to an object-alignment boundary, so an object whose fields plus a 12-byte header exceed a boundary by a small margin receives padding to reach the next one. Removing 4 bytes from the header can drop that object into the previous size class and **eliminate the padding as well**, which is why measured footprint reductions exceed the naive ratio of 4 bytes divided by the average object size.

## Enabling and verifying the flag

The feature is **off by default in JDK 25** and is enabled with a single launch flag; no source change and no recompilation are involved.

```bash
java -XX:+UseCompactObjectHeaders -jar app.jar
```

Verification matters because a flag that is silently ignored — for example under a JVM build that does not support it — produces no error and no saving. The GC initialisation log records the setting:

```bash
java -XX:+UseCompactObjectHeaders -Xlog:gc+init -version 2>&1 | grep -i "compact object headers"
```

Direct observation of the layout is available through [JOL](https://github.com/openjdk/jol), the Java Object Layout tool, which prints the byte-by-byte instance layout of a named class. Running it on the same class with and without the flag shows the header size changing:

```bash
java -XX:+UseCompactObjectHeaders -cp jol-cli.jar org.openjdk.jol.Main internals java.lang.Object
# header: 8 bytes  ->  instead of 12 without the flag
```

`java.lang.Object` is the sharpest test case: it declares no fields, so its entire instance size is header plus alignment padding, and any change in reported size is attributable to the header alone.

## Reported effect and its bound

On **SPECjbb2015**, the JEP and follow-up benchmarks report approximately **22% lower heap usage and roughly 8% lower CPU time**. Independent benchmarking by Balosin reports a footprint reduction of the same order, around 22%.

The mechanism behind the second-order effect is worth stating precisely, because the raw byte saving does not by itself explain a CPU reduction of that size. **A smaller live set means the collector reaches its allocation threshold less often and scans fewer bytes on each cycle**, and the same working set occupies fewer cache lines, so pointer-chasing traversals of an object graph incur fewer cache misses. The throughput improvement is a consequence of reduced memory traffic, not of the allocation path itself becoming cheaper.

Two costs are documented. The first is throughput: the JEP records a **worst-case throughput regression on some workloads**, arising from the extra work of decoding class identity out of the packed mark word on paths that previously read a dedicated word. The second is capacity: **the class-pointer encoding limits how many distinct classes can be loaded**, a limit high enough to be irrelevant for ordinary applications but not unbounded. Both costs are removed by dropping the flag; the change is reversible across a restart with no persistent state involved.

## Relevance to Scala workloads

Scala programs allocate a high ratio of small objects to total heap. Case classes, tuples, boxed primitives arising from generic code, and the many short-lived immutable values idiomatic Scala 3 produces are each individually small, so **the header is a larger fraction of each object's total size than it is in a typical Java workload built from wider mutable objects**. A pipeline that materialises millions of case-class instances therefore sits near the favourable end of the distribution for this flag.

Nothing in the build definition or the source changes. The flag is a JVM launch option, so it is applied where the JVM is started — a container entrypoint, a `JAVA_OPTS` environment variable, or an sbt fork option — and it applies identically to code compiled by any Scala version, because it changes the runtime's object representation rather than the bytecode.

A conservative evaluation procedure is to enable the flag on a single staging instance while leaving the remaining instances on stock JDK 25, then compare **heap occupancy after full collection** and **collection frequency** over a period of representative traffic. Heap-after-GC is the discriminating metric because it measures the live set rather than allocation rate, and the live set is exactly what the header change affects. Peak heap and allocation-rate counters will move much less, since the flag does not reduce the number of objects allocated.

## Pitfalls

- **Measuring allocation rate instead of live-set size shows no effect.** The flag reduces bytes per live object; it does not reduce the number of objects allocated, so an allocation-rate graph is nearly unchanged even when heap-after-GC drops by a fifth.
- **The flag is off by default in JDK 25 and produces no diagnostic when absent.** A deployment that sets it in a launch script the container does not use runs with 12-byte headers and reports normal, unremarkable numbers; only the `gc+init` log or a JOL layout dump distinguishes the two states.
- **Comparing JOL output across runs with different flag settings requires the flag on the JOL process itself.** JOL reports the layout of the JVM it is running in, so a JOL invocation without `-XX:+UseCompactObjectHeaders` prints 12-byte headers regardless of how the application under investigation is configured.
- **Objects dominated by their own fields show little relative benefit.** A 4-byte reduction against a large array payload or a wide record is a small fraction of instance size; the reported ~22% figures come from workloads whose live set is dominated by many small objects.
- **The class-encoding limit is a hard limit, not a soft degradation.** Applications that generate very large numbers of distinct classes at runtime, such as heavy dynamic proxy or code-generation use, are the population where the bounded class capacity is a real constraint rather than a theoretical one.
- **The reported figures come from specific benchmarks.** SPECjbb2015 results do not transfer to an arbitrary service; the per-workload effect is determined by the live set's object-size distribution, which is measurable only on that workload.
