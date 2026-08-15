---
title: "Compact Object Headers: Shrinking Every Object on the Heap (JEP 519)"
date: 2026-07-30
track: scala-jvm
summary: "Project Lilliput halves the JVM object header from 96–128 bits to 64 bits. JEP 450 shipped it as experimental in JDK 24; JEP 519 promoted it to a product feature in JDK 25. What changes in the header, how the flag is enabled, and how the savings are measured with JOL."
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

**Gist.** Every object on a 64-bit HotSpot Java Virtual Machine (JVM) carries a header of **96 to 128 bits** ahead of its fields, which for small objects exceeds the payload itself. Project Lilliput folds the class reference into the mark word to produce a single **64-bit** header, and JEP 519 makes that a product feature of JDK 25. The cost is that object layout and locking internals change, so native code, agents and `sun.misc.Unsafe` offset arithmetic that assume a fixed header size must be revalidated, and the flag remains opt-in.

## From experimental to product

The feature arrived in two steps, and the version distinction is load-bearing:

| JEP | Status | JDK | Flags required |
|-----|--------|-----|----------------|
| [450](https://openjdk.org/jeps/450) | Experimental | **24** | `-XX:+UnlockExperimentalVMOptions -XX:+UseCompactObjectHeaders` |
| [519](https://openjdk.org/jeps/519) | Product (still off by default) | **25** | `-XX:+UseCompactObjectHeaders` |

JEP 450 shipped compact headers as an *experimental* option in **JDK 24**, reachable only after unlocking experimental VM options. JEP 519 promoted the same mechanism to a **product feature in JDK 25**: the experimental gate is gone and the single flag `-XX:+UseCompactObjectHeaders` suffices. It remains **disabled by default in JDK 25**. The follow-up [JEP 534](https://openjdk.org/jeps/534) proposes enabling it by default in a later release.

The compressed summary: **JEP 519, JDK 25, product feature, opt-in flag.**

## What changes in the header

A default object header under compressed ordinary object pointers (compressed oops) on a 64-bit VM occupies two words:

```
| mark word (64 bits) | class word (32 bits) |   = 96 bits, padded to 128
```

The **mark word** holds the identity hash code, the lock-state bits, and the garbage-collector age bits. The **class word** is a compressed pointer to the object's `Klass` metadata, the runtime structure describing its type. Compact Object Headers encode the class reference *inside* the mark word, yielding one word:

```
| mark word + compressed class (64 bits) |   = 64 bits
```

Two consequences follow from that encoding. First, the class reference now shares a fixed-width word with the hash, lock and age bits, so the header is no longer a place where an external tool can assume a separate, independently addressable class slot. Second, because HotSpot aligns object sizes to an **8-byte boundary**, halving the header frequently removes an entire alignment slot rather than four scattered bytes: a bare `java.lang.Object` drops from **16 bytes to 8 bytes**. Header-dominated instances — linked-list nodes, small records, `HashMap.Node` — shrink by up to four bytes each, and by a further four whenever the alignment slot they occupied disappears too; an instance whose payload is a large array or a long string is barely affected. Because sizes stay 8-byte aligned, some classes do not shrink at all: a `java.lang.Integer` holding a 12-byte header plus a 4-byte field pads to 16 bytes, and an 8-byte header plus the same field still pads to 16. The size of the reduction is therefore a property of the object graph, not of the flag.

## Enabling and verifying the flag

JDK 25 or newer:

```bash
java -XX:+UseCompactObjectHeaders -jar app.jar
```

Whether the VM accepted the flag is checked by printing the final flag values; a value of `true` indicates the feature is active:

```bash
java -XX:+UseCompactObjectHeaders -XX:+PrintFlagsFinal -version \
  | grep UseCompactObjectHeaders
```

On JDK 24 the same flag requires the experimental gate:

```bash
java -XX:+UnlockExperimentalVMOptions -XX:+UseCompactObjectHeaders -jar app.jar
```

## Measuring the reduction with JOL

The header size is observable rather than a matter of trust. The [Java Object Layout](https://openjdk.org/projects/code-tools/jol/) (JOL) tool prints the exact byte layout of an instance. With the dependency `org.openjdk.jol:jol-core` on the classpath:

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

The probe is run twice, once without the flag and once with it:

```bash
java -cp jol-core.jar:. HeaderProbe                              # default headers
java -cp jol-core.jar:. -XX:+UseCompactObjectHeaders HeaderProbe # compact headers
```

Under default headers JOL reports `new Object()` as **16 bytes**: a 12-byte header plus 4 bytes of alignment padding. Under `-XX:+UseCompactObjectHeaders` the header line collapses to **8 bytes**, the padding disappears, and the instance total is **8 bytes**. For `Node`, the header row shrinks and every field offset drops by four bytes — which is precisely the reason offset-sensitive code must be retested.

### Implementation sketch (Scala)

Per-instance layout answers the question for one type. The aggregate question — how much of a live object graph is header — is answered by JOL's `GraphLayout`, which walks reachable references and totals their footprint. The sketch below measures one representative graph under whichever header mode the VM was started in; the difference between two runs is the quantity of interest.

```scala
import org.openjdk.jol.info.{ClassLayout, GraphLayout}

final case class Node(value: Int, next: Option[Node])

object HeaderCensus:
  /** Builds a deliberately header-dominated graph: payload is one Int per node. */
  def chain(n: Int): Node =
    (1 to n).foldLeft(Node(0, None))((tail, v) => Node(v, Some(tail)))

  def main(args: Array[String]): Unit =
    val root  = chain(100_000)
    val graph = GraphLayout.parseInstance(root)

    // headerSize() excludes fields and padding, so this isolates header cost.
    val perNodeHeader = ClassLayout.parseInstance(root).headerSize()
    val instances     = graph.totalCount()
    val bytes         = graph.totalSize()

    println(s"instances=$instances totalBytes=$bytes header=$perNodeHeader")
    println(s"headerBytes=${instances * perNodeHeader}")
    println(graph.toFootprint())
```

`GraphLayout.parseInstance` counts each distinct reachable object once, so shared structure is not double-billed. Running the same class under both header modes and differencing `totalBytes` gives a measurement rather than an estimate.

## Confirming it at scale from garbage-collection logs

Aggregate live-set size is the figure that governs heap sizing. It is compared across two runs using unified garbage-collection (GC) logging:

```bash
java -Xlog:gc*:file=default.log  -jar app.jar
java -Xlog:gc*:file=compact.log  -XX:+UseCompactObjectHeaders -jar app.jar
```

A full collection is triggered under a representative load and the live heap reported after that collection is compared. The published figure comes from one benchmark: JEP 450 reports that on SPECjbb2015 compact headers reduced heap size by up to **22%**, together with reductions in central processing unit (CPU) time and garbage-collection pressure. That single measurement does not transfer to another application, because the achievable reduction depends on how header-dominated the object graph is; only a differenced pair of runs on the workload in question settles it.

## Pitfalls

- **The flag is off by default in JDK 25.** An application upgraded to JDK 25 gains nothing until `-XX:+UseCompactObjectHeaders` is passed; the symptom is unchanged heap figures after an upgrade that was expected to shrink them.
- **Field offsets move.** Code performing `sun.misc.Unsafe` field-offset arithmetic that assumes a 12-byte header reads or writes the wrong bytes under compact headers; the symptom is corrupted field values or a crash rather than a clean error.
- **Native code and agents that hard-code header size break silently.** Anything parsing object layout outside the VM — JNI code, profilers, serialization that walks raw memory — must be revalidated, because the header no longer contains a separate class word.
- **Measuring a payload-dominated graph shows nothing.** A benchmark built from large arrays and long strings reports a reduction near zero, which is a property of the workload, not evidence the flag failed to apply; `-XX:+PrintFlagsFinal` distinguishes the two cases.
- **JDK 24 requires the experimental unlock.** Passing `-XX:+UseCompactObjectHeaders` alone on JDK 24 causes the VM to refuse to start rather than to ignore the flag.
