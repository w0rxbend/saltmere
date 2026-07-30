---
title: "Compact Object Headers: Trimming the JVM's Per-Object Tax with Project Lilliput"
date: 2026-07-30
track: scala-jvm
summary: "Every Java object carries a 96–128 bit header before your first field. Project Lilliput's compact headers pack that into a single 64-bit word, cutting live-heap footprint by 10–20% and shrinking the per-object cache tax. Here's the layout change and how to turn it on in JDK 24/25."
reading_time: 5
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

Every object on the Java heap pays a fixed tax before it stores a single field of your own: the object header. On a 64-bit HotSpot JVM that header is **between 96 bits (12 bytes) and 128 bits (16 bytes)**, depending on configuration. For a workload dominated by small objects — a graph of `Long` keys, boxed values in a cache, a few-field domain record — the header can be a larger fraction of the object than your data. Project Lilliput's [compact object headers](https://openjdk.org/jeps/450) collapse that down to a single **64-bit** word.

## What the classic header holds

The traditional header is two words:

- **Mark word (64 bits):** mutable, per-object runtime state — the identity hash code (lazily computed), GC age bits, and the lock tag bits that let the word morph into a pointer to a stack lock record or an inflated monitor. In its plain "unlocked" state it looks roughly like:
  ```
  [ unused | identity hashcode (31) | unused | GC age (4) | tag (2) ]
  ```
- **Class word:** a pointer to the object's `Klass` metadata. With `-XX:+UseCompressedClassPointers` (the default) this is 32 bits; without it, 64. This word is *never* overwritten, so an object's type is always readable — the GC and every virtual call depend on it.

So a compressed-oops JVM lands at 96 bits (64 + 32), and disabling compressed class pointers pushes it to 128. Add object alignment padding (8-byte default) and a two-field object frequently rounds up so that a third or more of its footprint is header.

## What compact headers do

The insight behind [JEP 450](https://openjdk.org/jeps/450) is that the mark word is mostly empty most of the time, and the class pointer doesn't need a full 32 bits. Lilliput merges both into one 64-bit word:

```
[ compressed class pointer (22) | hashcode | Valhalla-reserved | GC age (4) | tag ]
```

The compressed class pointer is squeezed from 32 bits down to **22 bits** of class-identifying information, packed alongside the hash, GC age, and lock tag in the same word. Twenty-two bits still addresses millions of loaded classes — far more than any real application defines — so the trade-off is invisible in practice. The header goes from 12 bytes to **8 bytes** per object.

## The wins: footprint, cache, throughput

Four bytes per object sounds trivial until you multiply by a live set of hundreds of millions of objects. The JEP reports that early adopters saw **live data reduced by 10–20%**. That's not just RAM saved — it's second-order:

- **GC does less work.** Less live data means less to mark, copy, and relocate every cycle, and lower allocation pressure between cycles.
- **Better cache density.** More objects per cache line means fewer misses walking a hot data structure. This is where the throughput gains come from even when memory isn't the bottleneck.

Project Lilliput's own [performance testing](https://wiki.openjdk.org/display/lilliput/Performance+Testing+for+JEP+450:+Compact+Object+Headers) on **SPECjbb2015** measured roughly **22% less heap usage and 8% less CPU time**. The upside isn't free everywhere: worst-case throughput overhead is small (the feature caps regressions at a few percent), but small-object-heavy workloads tend to come out net ahead.

## Enabling it

The flag changed status between releases, which matters for your launch scripts:

```bash
# JDK 24 — experimental (JEP 450), must unlock first
java -XX:+UnlockExperimentalVMOptions -XX:+UseCompactObjectHeaders -jar app.jar

# JDK 25 — product feature (JEP 519), no unlock needed
java -XX:+UseCompactObjectHeaders -jar app.jar
```

[JEP 519](https://openjdk.org/jeps/519) promoted the feature from *experimental* to a *product* option in **JDK 25** — but note it is **still disabled by default**. You opt in explicitly on both releases; only the ceremony differs. (A separate follow-up, JEP 534, tracks the eventual goal of making it the default; that hasn't shipped.)

## Observing the effect with JOL

Don't take the byte count on faith — measure it. [JOL (Java Object Layout)](https://openjdk.org/projects/code-tools/jol/) reads the real in-memory layout. Grab `jol-cli` and inspect a bare `Object` with and without the flag:

```bash
# Classic header: "object header" reported as 12 bytes
java -jar jol-cli.jar internals java.lang.Object

# Compact header: same command, header now 8 bytes
java -XX:+UseCompactObjectHeaders -jar jol-cli.jar internals java.lang.Object
```

Or from code, which also shows how padding shifts your first field's offset:

```java
import org.openjdk.jol.info.ClassLayout;

record Point(int x, int y) {}

public class Layout {
    public static void main(String[] args) {
        System.out.println(ClassLayout.parseInstance(new Point(1, 2)).toPrintable());
    }
}
```
```bash
java -XX:+UseCompactObjectHeaders -cp jol-core.jar:. Layout
```

With compact headers the `OBJECT HEADER` block drops from 12 to 8 bytes; for a two-`int` record that alone can move it from a 16-byte to a 16-byte instance *with room to spare* — meaning objects that previously spilled into a larger alignment bucket now don't.

## Confirming heap-level impact

For a whole-application picture, compare live-set size across a run with GC logging rather than trusting a micro-benchmark:

```bash
java -XX:+UseG1GC -Xlog:gc+heap=info:file=gc.log:time,uptime -jar app.jar
```

Run once with and once without `-XX:+UseCompactObjectHeaders`, drive identical load, and compare the post-GC live-heap occupancy lines. On an object-dense workload you should see the live set shrink into the 10–20% range the JEP predicts.

**Try next:** take a service that allocates many small objects, run it twice under identical load — once plain, once with `-XX:+UseCompactObjectHeaders` — capturing `-Xlog:gc+heap=info`, then diff the post-GC live-heap sizes and GC CPU time to see whether your workload lands in the 10–20% footprint-reduction band.
