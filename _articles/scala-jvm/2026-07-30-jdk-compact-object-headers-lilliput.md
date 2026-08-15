---
title: "Compact Object Headers: Trimming the JVM's Per-Object Tax with Project Lilliput"
date: 2026-07-30
track: scala-jvm
summary: "Every Java object carries a 96–128 bit header before its first field. Project Lilliput's compact headers pack that into a single 64-bit word, reducing live-heap footprint by a reported 10–20% and raising the number of objects per cache line. The layout change, the flag's status in JDK 24 and 25, and how to measure the effect."
reading_time: 6
tags: [jvm, memory, lilliput, jdk24, jdk25, performance, gc, jol]
sources:
  - title: "JEP 450: Compact Object Headers (Experimental)"
    url: "https://openjdk.org/jeps/450"
  - title: "JEP 519: Compact Object Headers"
    url: "https://openjdk.org/jeps/519"
  - title: "Performance Testing for JEP 450 — Project Lilliput (OpenJDK Wiki)"
    url: "https://wiki.openjdk.org/display/lilliput/Performance+Testing+for+JEP+450:+Compact+Object+Headers"
  - title: "Reduce Object Header Size and Save Memory in Java 25 (Baeldung)"
    url: "https://www.baeldung.com/java-object-header-reduced-size-save-memory"
  - title: "Java 25 Integrates Compact Object Headers with JEP 519 (InfoQ)"
    url: "https://www.infoq.com/news/2025/06/java-25-compact-object-headers/"
---

**Gist.** Every object on a 64-bit HotSpot Java Virtual Machine (JVM) heap carries a header of **96 bits (12 bytes) to 128 bits (16 bytes)** before any declared field, so object-dense workloads spend a large fraction of the live set on metadata. Project Lilliput's [compact object headers](https://openjdk.org/jeps/450) merge the mark word and the class word into a **single 64-bit word**, which [JEP 450](https://openjdk.org/jeps/450) reports reduces live data by **10–20%**. The cost is a narrower compressed class pointer — **22 bits** of class-identifying information instead of 32 — plus a small worst-case throughput regression, and the feature remains opt-in.

## What the classic header holds

The traditional header occupies two words.

- **Mark word (64 bits).** Mutable per-object runtime state: the identity hash code, which is computed lazily on first request; garbage collector (GC) age bits; and the lock tag bits. The tag is what makes the word polymorphic — depending on the lock state, the remaining bits are interpreted as hash and age, as a pointer to a stack-allocated lock record, or as a pointer to an inflated monitor. In the plain unlocked state the layout is roughly:
  ```
  [ unused | identity hashcode (31) | unused | GC age (4) | tag (2) ]
  ```
  Because the hash code shares the word with the lock encoding, **requesting `identityHashCode` on an object interacts with that word's state machine**; the hash, once materialised, must survive every later lock transition.
- **Class word.** A pointer to the object's `Klass` metadata. With `-XX:+UseCompressedClassPointers` (the default) this is 32 bits; without it, 64. This word is **never overwritten**, so the object's type is readable at any instant — a property the GC relies on when it walks an object to find its reference fields, and which virtual dispatch relies on to reach the vtable.

A JVM with compressed class pointers therefore lands at 96 bits (64 + 32); disabling them pushes the header to 128 bits. Object alignment — **8 bytes by default** — then rounds the instance size up, so a small object can spend a third or more of its footprint on the header and its padding.

## What compact headers change

[JEP 450](https://openjdk.org/jeps/450) folds both words into one 64-bit word:

```
[ compressed class pointer (22) | hashcode | unused | GC age (4) | tag ]
```

The compressed class pointer is narrowed from 32 bits to **22 bits**, sharing the word with the hash code, the GC age, and the lock tag. The header goes from **12 bytes to 8 bytes** per object. The invariant from the two-word layout is preserved: the class bits must remain readable regardless of lock state, so the encodings that previously displaced the entire mark word now have to leave the class field intact.

The narrowing bounds the number of distinct loaded classes a single JVM can address in this encoding at 2^22 minus whatever values the encoding reserves. That is the concrete price of the change; JEP 450 documents the narrowing rather than a motive for the specific width.

## Measured effect

Four bytes per object is significant only in aggregate: across a live set of hundreds of millions of objects it moves gigabytes. The reduction propagates in two directions.

- **GC work scales with live data.** A smaller live set means fewer bytes to mark, copy and relocate per cycle, and lower allocation pressure between cycles.
- **Cache density.** More objects fit per cache line, so a traversal of a hot data structure issues fewer misses. This is the path by which throughput improves even when the heap is not the constraint.

Project Lilliput's [performance testing](https://wiki.openjdk.org/display/lilliput/Performance+Testing+for+JEP+450:+Compact+Object+Headers) on **SPECjbb2015** measured roughly **22% less heap usage and 8% less CPU time**. The effect is not uniformly positive: worst-case throughput regressions are reported as small, on the order of a few percent, and workloads dominated by few large objects or by large arrays gain little, because the header is already a negligible fraction of those instances.

## Enabling it

The flag's status differs across releases, which affects launch scripts:

```bash
# JDK 24 — experimental (JEP 450), requires unlocking first
java -XX:+UnlockExperimentalVMOptions -XX:+UseCompactObjectHeaders -jar app.jar

# JDK 25 — product option (JEP 519), no unlock required
java -XX:+UseCompactObjectHeaders -jar app.jar
```

[JEP 519](https://openjdk.org/jeps/519) promoted the feature from *experimental* to a *product* option in **JDK 25**, where it remains **disabled by default**. Both releases require an explicit opt-in; only the unlock flag differs. Making the feature the default is discussed as future work; no cited source records it as shipped.

## Observing the layout

[JOL (Java Object Layout)](https://openjdk.org/projects/code-tools/jol/) reports the real in-memory layout rather than a computed estimate. Inspecting a bare `Object` under both settings shows the header size directly:

```bash
# Classic header: "object header" reported as 12 bytes
java -jar jol-cli.jar internals java.lang.Object

# Compact header: same command, header now 8 bytes
java -XX:+UseCompactObjectHeaders -jar jol-cli.jar internals java.lang.Object
```

### Implementation sketch (Scala)

The same measurement from code also exposes where the first declared field starts and how much padding the alignment rule adds. `ClassLayout.parseInstance` returns the layout of an actual instance, so the numbers reflect the flags the JVM was launched with.

```scala
import org.openjdk.jol.info.ClassLayout

final case class Point(x: Int, y: Int)

@main def layout(): Unit =
  val cases: List[(String, AnyRef)] = List(
    "Object"     -> new Object,
    "Point(1,2)" -> Point(1, 2),
    "boxed Long" -> java.lang.Long.valueOf(1L)
  )

  for (name, instance) <- cases do
    val l = ClassLayout.parseInstance(instance)
    // headerSize() is the metadata prefix; instanceSize() includes alignment padding
    println(f"$name%-12s header=${l.headerSize()}%2d bytes  instance=${l.instanceSize()}%2d bytes")
    println(l.toPrintable)
```

Run it twice under identical classpaths, once with and once without the flag:

```bash
scala-cli run layout.scala
scala-cli run --java-opt -XX:+UseCompactObjectHeaders layout.scala
```

The `OBJECT HEADER` block drops from 12 bytes to 8. Whether the *instance* shrinks depends on alignment: freeing four header bytes only reduces the instance size when the object's fields do not already occupy the padding those bytes leave behind.

## Confirming heap-level impact

A micro-benchmark measures one object; the deployment question is the live set. GC logging answers it:

```bash
java -XX:+UseG1GC -Xlog:gc+heap=info:file=gc.log:time,uptime -jar app.jar
```

Two runs under identical load, one with `-XX:+UseCompactObjectHeaders` and one without, produce comparable post-GC live-heap occupancy lines. An object-dense workload is the case in which the live set plausibly falls into the 10–20% band JEP 450 reports; a workload dominated by arrays or by few large objects is not.

## Pitfalls

- **Assuming the flag is on in JDK 25.** JEP 519 made it a product option, not a default; a service upgraded to JDK 25 without the flag has the classic 12-byte header and shows no footprint change.
- **Carrying a JDK 24 launch script forward unchanged.** `-XX:+UnlockExperimentalVMOptions` is required on JDK 24 and unnecessary on 25; omitting it on 24 causes the JVM to reject `-XX:+UseCompactObjectHeaders` at startup rather than silently ignore it.
- **Expecting every instance to shrink by four bytes.** Object alignment is 8 bytes by default, so an instance size only drops when the freed header bytes are not absorbed by padding the object already carried.
- **Extrapolating SPECjbb2015 numbers.** The measured 22% heap and 8% CPU reductions describe one benchmark's object-size distribution; a workload built on large arrays has a header fraction near zero and gains correspondingly little.
- **Reading layout numbers from a JVM launched without the flag.** JOL reports the layout of the JVM it runs inside, so the flag must be passed to the JOL process itself, not to the application being studied.
- **Treating the 22-bit class pointer as unbounded.** It encodes strictly fewer distinct classes than the 32-bit form; an environment that generates classes dynamically without bound is the case where that ceiling, rather than throughput, is the operative limit.
