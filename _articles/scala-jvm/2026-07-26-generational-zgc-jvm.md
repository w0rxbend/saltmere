---
title: "Generational ZGC: Why Sub-Millisecond Pauses Needed a Young Generation"
date: 2026-07-26
track: scala-jvm
summary: "Non-generational ZGC could already hit sub-millisecond pauses, but it wasted work re-scanning long-lived objects every cycle. Generational ZGC fixes that with the weak generational hypothesis — and as of JDK 24 it's the only mode left."
reading_time: 5
tags: [jvm, gc, zgc, java, jdk21, performance, tuning]
sources:
  - title: "JEP 439: Generational ZGC"
    url: "https://openjdk.org/jeps/439"
  - title: "JEP 474: ZGC: Generational Mode by Default"
    url: "https://openjdk.org/jeps/474"
  - title: "JEP 490: ZGC: Remove the Non-Generational Mode"
    url: "https://openjdk.org/jeps/490"
  - title: "Bending pause times to your will with Generational ZGC (Netflix TechBlog)"
    url: "https://netflixtechblog.com/bending-pause-times-to-your-will-with-generational-zgc-256629c9386b"
  - title: "The Z Garbage Collector — HotSpot GC Tuning Guide"
    url: "https://docs.oracle.com/en/java/javase/23/gctuning/z-garbage-collector.html"
---

ZGC has been able to advertise sub-millisecond pauses since JDK 15 made it production-ready. So why did OpenJDK spend three more release cycles bolting a young generation onto it? Because "low pause times" and "low overhead" are different problems, and the original single-generation ZGC only solved the first one well.

## The problem single-gen ZGC didn't solve

Non-generational ZGC treats every object in the heap identically: each collection cycle marks, relocates, and remaps the entire live set, young objects and decade-old singletons alike. That gives you flat, predictable pause times regardless of heap size, but it means CPU and memory bandwidth scale with total heap occupancy, not with allocation rate. High-allocation-rate services — the common case for JVM backends doing JSON parsing, protobuf decoding, or Kafka consumption — pay for scanning old, stable objects on every single cycle.

[JEP 439](https://openjdk.org/jeps/439), targeted at **JDK 21**, fixes this by applying the **weak generational hypothesis**: most objects die young, and the ones that survive tend to stay alive a long time. Splitting the heap into a young generation (collected frequently and cheaply) and an old generation (collected rarely) lets ZGC do less total work for the same throughput, while keeping the same latency guarantees non-generational ZGC already had.

## Timeline: from opt-in to only-in

| JDK | JEP | What changed |
|---|---|---|
| 21 (Sep 2023) | JEP 439 | Generational ZGC ships as opt-in, alongside the original mode |
| 23 (Sep 2024) | JEP 474 | `-XX:+UseZGC` alone now enables *generational* mode by default; non-generational requires an explicit, deprecated flag |
| 24 (Mar 2025) | JEP 490 | Non-generational mode is removed entirely; `-XX:+UseZGC` is the only ZGC and is always generational |

On JDK 25 (the current LTS), there is no "choice" to make: ZGC is generational, full stop. On JDK 21 or 22 you still need to opt in explicitly.

## Enabling it

```
# JDK 21 or 22: opt in explicitly
java -XX:+UseZGC -XX:+ZGenerational -jar app.jar

# JDK 23: UseZGC alone is enough (ZGenerational defaults to true;
# -XX:-ZGenerational still works but prints a deprecation warning)
java -XX:+UseZGC -jar app.jar

# JDK 24+: only mode that exists — ZGenerational flag is gone
java -XX:+UseZGC -jar app.jar
```

If you maintain a fleet spanning JDK 21–25, drop the `ZGenerational` flag entirely and let each JVM pick its own default — it collapses to the same behavior on 23+ anyway.

## Colored pointers and barriers, briefly

ZGC's trick for concurrent relocation without stop-the-world compaction is the **colored pointer**: on 64-bit systems, a handful of otherwise-unused bits in every object reference encode metadata — whether the referent is marked, remapped, or still needs relocating — instead of storing that state in a separate side table. A **load barrier**, a few extra instructions injected at every heap reference load, checks those color bits and, if the object has moved or hasn't been marked yet, fixes the reference up on the spot before the application ever sees a stale pointer.

Generational ZGC adds a second mechanism on top: a **store barrier**. Because the young and old generations are collected on independent cycles, ZGC needs to track old-to-young references (the classic remembered-set problem) without a full heap scan. The store barrier records those cross-generational writes cheaply as they happen, so a young-generation collection only has to consult that recorded set instead of walking the entire old generation. This is the mechanism that turns "collect everything every time" into "collect what's likely garbage, cheaply, most of the time."

## Generational vs. non-generational, in practice

| Aspect | Non-generational ZGC (removed in JDK 24) | Generational ZGC |
|---|---|---|
| Pause times | Sub-millisecond | Sub-millisecond (same guarantee) |
| CPU/bandwidth cost | Scales with total heap size | Scales with allocation rate |
| Best for | Mostly-static heaps, huge caches | Typical request/response services, high allocation churn |
| Barriers | Load barrier only | Load barrier + store barrier |
| Status on JDK 25 | Not available | Default and only mode |

## Tuning: soft max heap and allocation stalls

ZGC has almost no traditional generation-sizing knobs — that's the point — but two flags matter in practice.

`-XX:SoftMaxHeapSize` sets a *preferred* ceiling below `-Xmx`. ZGC tries to stay under it, and only grows past it when the alternative is an **allocation stall** (a thread blocking because there's no free memory and GC can't keep up) or an `OutOfMemoryError`. This is the right way to give a container both a low steady-state footprint and headroom for spikes:

```
# Keep heap around 4G in steady state, but allow growth to 6G under load
java -XX:+UseZGC -Xmx6g -XX:SoftMaxHeapSize=4g -jar app.jar
```

It's also mutable at runtime without a restart:

```
jcmd <pid> VM.set_flag SoftMaxHeapSize 5g
```

To see whether you're actually hitting allocation stalls, read the GC log rather than guessing:

```
java -XX:+UseZGC -Xlog:gc,gc+stats=debug:file=gc.log:time,uptime -jar app.jar
```

Grep the resulting log for `Allocation Stall` — any non-zero count under normal load means the heap is undersized relative to allocation rate, or `SoftMaxHeapSize` is set too aggressively low. The `gc` tag alone gives you per-cycle pause and reclaimed-memory lines; `gc+stats` (dumped at JVM exit, or periodically with `-Xlog:gc+stats*=debug`) gives cumulative allocation-rate and stall counters that are far more useful for capacity planning than eyeballing individual cycles.

**Try next:** run the same service under load with `-XX:+UseZGC -Xmx4g -XX:SoftMaxHeapSize=2g -Xlog:gc*:file=gc.log:time,uptime`, then grep `gc.log` for `Allocation Stall` — if you see any, bump `SoftMaxHeapSize` toward `-Xmx` and rerun until the stall count hits zero.
