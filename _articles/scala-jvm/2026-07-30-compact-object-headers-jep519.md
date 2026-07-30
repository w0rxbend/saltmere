---
title: "Compact Object Headers: Shrinking Every Object on the Heap (JEP 519)"
date: 2026-07-30
track: scala-jvm
summary: "Project Lilliput halves the JVM object header from 96–128 bits to 64 bits. JEP 450 shipped it as experimental in JDK 24; JEP 519 promoted it to a product feature in JDK 25. Here is what actually changes, how to turn it on, and how to measure the savings with JOL."
reading_time: 6
tags: [jvm, gc, memory, project-lilliput, jdk25]
sources:
  - title: "JEP 519: Compact Object Headers"
    url: "https://openjdk.org/jeps/519"
  - title: "JEP 450: Compact Object Headers (Experimental)"
    url: "https://openjdk.org/jeps/450"
  - title: "Java 25 Integrates Compact Object Headers with JEP 519 (InfoQ)"
    url: "https://www.infoq.com/news/2025/06/java-25-compact-object-headers/"
  - title: "Compact Object Headers in Java (JEP 519) — HappyCoders"
    url: "https://www.happycoders.eu/java/compact-object-headers/"
  - title: "JEP 534: Compact Object Headers by Default"
    url: "https://openjdk.org/jeps/534"
---

Every object you allocate on the JVM carries a header before its fields. On a 64-bit HotSpot with compressed class pointers, that header is **between 96 and 128 bits** — a 64-bit *mark word* (identity hash, lock state, GC age bits) plus a 32-bit *class word* pointing at the object's `Klass`. For a small object — a boxed `Integer`, a two-field record, a linked-list node — the header can be larger than the payload. Project Lilliput's answer is to fold that class information into the mark word and get the whole header down to **64 bits**. On header-heavy workloads that trims live heap data by roughly 10–20%.

## From experimental to product

This feature arrived in two steps, and getting the version right matters:

| JEP | Status | JDK | Flags required |
|-----|--------|-----|----------------|
| [450](https://openjdk.org/jeps/450) | Experimental | **24** | `-XX:+UnlockExperimentalVMOptions -XX:+UseCompactObjectHeaders` |
| [519](https://openjdk.org/jeps/519) | Product (still off by default) | **25** | `-XX:+UseCompactObjectHeaders` |

JEP 450 shipped compact headers as an *experimental* option in **JDK 24** — you had to unlock experimental VM options to touch it. JEP 519 promoted the same mechanism to a **product feature in JDK 25**: it is now supported and stable, the experimental unlock is gone, and you enable it with the single flag `-XX:+UseCompactObjectHeaders`. It remains **disabled by default** in JDK 25 — you opt in. (The follow-up [JEP 534](https://openjdk.org/jeps/534) proposes flipping the default on, targeting a later release.)

If you take one fact from this article: **JEP 519, JDK 25, product feature, opt-in flag.**

## What actually changes in the header

A default object header under compressed oops looks like this on a 64-bit VM:

```
| mark word (64 bits) | class word (32 bits) |   = 96 bits, padded to 128
```

The mark word holds the identity hash code, biased/thin-lock bits, and GC age. The class word is a compressed pointer to the object's `Klass` metadata. Compact Object Headers squeeze the class reference *into* the mark word, producing a single **64-bit** header:

```
| mark word + compressed class (64 bits) |   = 64 bits
```

Because HotSpot pads objects to an 8-byte boundary, the practical payoff is that many small objects drop an entire alignment slot. A bare `java.lang.Object` goes from **16 bytes to 8 bytes**. Anything header-dominated — nodes, boxed primitives, tiny records, `HashMap.Node` — shrinks proportionally, which is why real applications see live-set reductions rather than a rounding error.

## Enabling it

Requires JDK 25 or newer:

```bash
java -XX:+UseCompactObjectHeaders -jar app.jar
```

Confirm the VM accepted it (a supported product flag prints cleanly; a value of `true` means it is active):

```bash
java -XX:+UseCompactObjectHeaders -XX:+PrintFlagsFinal -version \
  | grep UseCompactObjectHeaders
```

On JDK 24 the same flag needs the experimental gate — useful only if you are still on 24:

```bash
java -XX:+UnlockExperimentalVMOptions -XX:+UseCompactObjectHeaders -jar app.jar
```

## Measuring the shrink with JOL

Don't take the header size on faith — measure it. The [Java Object Layout](https://openjdk.org/projects/code-tools/jol/) (JOL) tool prints the exact byte layout of an instance. Add the dependency (Maven coordinates `org.openjdk.jol:jol-core`) and run a one-liner:

```java
import org.openjdk.jol.info.ClassLayout;

public final class HeaderProbe {
    static final class Node { int value; Node next; }

    public static void main(String[] args) {
        System.out.println(ClassLayout.parseInstance(new Object()).toPrintable());
        System.out.println(ClassLayout.parseInstance(new Node()).toPrintable());
    }
}
```

Run it twice, once without the flag and once with it:

```bash
java -cp jol-core.jar:. HeaderProbe                              # default headers
java -cp jol-core.jar:. -XX:+UseCompactObjectHeaders HeaderProbe # compact headers
```

With default headers, JOL reports `new Object()` as **16 bytes** (12-byte header + 4 bytes padding). With `-XX:+UseCompactObjectHeaders`, the header collapses to **8 bytes** and the instance is **8 bytes** total — the object-header line in the printable layout drops from 12 to 8 bytes, and the alignment padding disappears. For the `Node`, watch the header row shrink and the field offsets slide up by four bytes.

## Confirming it at scale with GC logs

Per-object bytes are convincing; aggregate live-set is what your ops team cares about. Compare live data after a full GC with and without the flag using unified GC logging:

```bash
java -Xlog:gc*:file=default.log  -jar app.jar
java -Xlog:gc*:file=compact.log  -XX:+UseCompactObjectHeaders -jar app.jar
```

Trigger a full collection under a representative load, then diff the "live" heap reported after the collection. Reported numbers from early adopters land in the expected band: JEP 519 itself cites **10–20%** reductions in live data, Amazon measured up to **22% less heap** on SPECjbb2015 (plus throughput gains from better cache locality), and Alibaba reported roughly **5–10%**. The exact figure depends entirely on how header-dominated your object graph is — an app full of large arrays and strings sees less; one full of tiny nodes and boxed values sees more.

## Caveats worth knowing

- **Off by default in JDK 25.** You must pass the flag. Bake it into your `JAVA_TOOL_OPTIONS` or launch script and treat it as a deliberate config change, not a free upgrade.
- **Test before trusting.** It changes object layout and locking internals. Native code or agents that assume a fixed header size, and anything doing `Unsafe` field-offset arithmetic against the header, deserve a regression pass.
- **Smaller headers, better cache behavior.** The win isn't only bytes: fitting more objects per cache line reduces misses, which is where the reported throughput improvements come from.
- **The default is coming.** JEP 534 aims to enable compact headers out of the box in a future release, so getting comfortable with the flag now is a low-cost dress rehearsal.

**Try next:** On a JDK 25 build, run your service for an hour with `-Xlog:gc*` both with and without `-XX:+UseCompactObjectHeaders`, diff the post-full-GC live heap, and confirm the per-object shrink on your hottest types with `ClassLayout.parseInstance(...).toPrintable()`.
