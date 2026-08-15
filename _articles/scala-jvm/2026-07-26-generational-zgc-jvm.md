---
title: "Generational ZGC: Why Sub-Millisecond Pauses Needed a Young Generation"
date: 2026-07-26
track: scala-jvm
summary: "Non-generational ZGC already reached sub-millisecond pauses but re-scanned long-lived objects every cycle. Generational ZGC applies the weak generational hypothesis, and as of JDK 24 it is the only mode that remains."
reading_time: 6
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

**Gist.** The Z Garbage Collector (ZGC) reached sub-millisecond pauses while still marking, relocating and remapping the entire live set on every cycle, so its central-processing-unit (CPU) and memory-bandwidth cost tracked total heap occupancy rather than allocation rate. Generational ZGC ([JEP 439](https://openjdk.org/jeps/439), JDK 21) splits the heap into a young generation collected frequently and an old generation collected rarely, so most cycles touch only the region where most objects die. The cost is a second write-side mechanism — a **store barrier** that records old-to-young references — plus the cross-generational bookkeeping that barrier feeds.

## The problem single-generation ZGC did not solve

Non-generational ZGC treats every object identically: each collection cycle marks, relocates and remaps the whole live set, newly allocated objects and long-lived singletons alike. Pause time stays flat and independent of heap size, because the marking and relocation work is concurrent with the application. What does not stay flat is **total work per cycle, which scales with live-set size rather than with garbage produced**. A service that allocates heavily and discards almost all of it — JavaScript Object Notation (JSON) parsing, protocol-buffer decoding, Kafka consumption — repeatedly re-traverses a stable old population to reclaim a small young one.

The **weak generational hypothesis** states that most objects die young and that objects surviving several collections tend to remain reachable for a long time. Under that distribution, restricting the common cycle to the young generation reclaims most of the available garbage for a fraction of the traversal, while leaving ZGC's latency behaviour unchanged: the pause-time guarantee is a property of the concurrent marking and relocation design, not of the generation split.

## Timeline: from opt-in to only mode

| JDK | JEP | Change |
|---|---|---|
| 21 (Sep 2023) | JEP 439 | Generational ZGC ships as opt-in, alongside the original mode |
| 23 (Sep 2024) | JEP 474 | `-XX:+UseZGC` alone enables *generational* mode; non-generational requires an explicit, deprecated flag |
| 24 (Mar 2025) | JEP 490 | Non-generational mode is removed; `-XX:+UseZGC` is always generational |

On JDK 25 there is no selection to make. On JDK 21 or 22 generational mode is opt-in.

```
# JDK 21 or 22: opt in explicitly
java -XX:+UseZGC -XX:+ZGenerational -jar app.jar

# JDK 23: UseZGC alone suffices (ZGenerational defaults to true;
# -XX:-ZGenerational still works but prints a deprecation warning)
java -XX:+UseZGC -jar app.jar

# JDK 24+: the only mode that exists — ZGenerational is obsolete and ignored
java -XX:+UseZGC -jar app.jar
```

Across a fleet spanning JDK 21–25, omitting `ZGenerational` leaves each JVM on its own default, which is generational from 23 onward.

## Colored pointers, load barriers, store barriers

ZGC relocates objects concurrently with the running application rather than compacting during a stop-the-world pause. The enabling representation is the **colored pointer**: on 64-bit systems, otherwise-unused bits inside every object reference encode metadata — whether the referent has been marked, whether it has been remapped, whether it still requires relocation — instead of that state living in a separate side table.

The invariant this maintains is that **the application never observes a stale reference**. It is enforced by the **load barrier**, a short instruction sequence injected at every heap reference load. The barrier inspects the color bits of the loaded reference; if they indicate the object has moved or has not yet been marked, the barrier performs the required fix-up — following the forwarding information, or marking the object — and returns a reference the application can use directly. Correction happens at the point of use, so relocation need not be atomic with respect to the whole heap.

Generational ZGC adds a **store barrier** on reference writes. Because young and old generations are collected on independent cycles, a young collection must treat old-generation references into the young generation as roots; otherwise it would have to traverse the old generation to find them, which is the cost the split exists to avoid. This is the remembered-set problem. The store barrier records cross-generational writes as they occur, so a young collection consults the recorded set instead of scanning the old generation. The failure mode of an unsound remembered set is not a slow collection but **a young object freed while an old object still points at it**, and the barrier is what makes the set sound.

### Implementation sketch (Scala)

The following models the remembered-set invariant — not ZGC's internal representation, which is a JVM implementation detail. The point is that the write path, not the collection path, is where cross-generational edges are captured.

```scala
final case class Ref(id: Long)

enum Gen:
  case Young, Old

trait Heap:
  def genOf(r: Ref): Gen
  def fieldsOf(r: Ref): Iterable[Ref]

final class RememberedSet:
  private var edges: Set[Ref] = Set.empty   // old-gen holders of young refs

  /** Store barrier: runs on every reference write. */
  def onStore(holder: Ref, value: Ref, heap: Heap): Unit =
    if heap.genOf(holder) == Gen.Old && heap.genOf(value) == Gen.Young then
      edges += holder

  /** Young roots = thread/stack roots plus recorded old-gen holders. */
  def youngRoots(stackRoots: Set[Ref], heap: Heap): Set[Ref] =
    stackRoots.filter(heap.genOf(_) == Gen.Young) ++
      edges.flatMap(heap.fieldsOf).filter(heap.genOf(_) == Gen.Young)

  /** Holders promoted or collected must leave the set, or it grows without bound. */
  def drop(holder: Ref): Unit = edges -= holder
```

Dropping the barrier makes `youngRoots` incomplete, and incomplete young roots free reachable objects.

## Generational versus non-generational

| Aspect | Non-generational ZGC (removed in JDK 24) | Generational ZGC |
|---|---|---|
| Pause times | Sub-millisecond | Sub-millisecond (same guarantee) |
| CPU and bandwidth cost | Scales with total live set | Scales with allocation rate |
| Suited to | Mostly-static heaps, large caches | Request/response services with high allocation churn |
| Barriers | Load barrier only | Load barrier and store barrier |
| Status on JDK 25 | Unavailable | Default and only mode |

## Tuning: soft maximum heap size and allocation stalls

ZGC exposes few generation-sizing controls. Two flags carry most of the practical weight.

`-XX:SoftMaxHeapSize` sets a *preferred* ceiling below `-Xmx`. ZGC attempts to remain under it and exceeds it only when the alternative is an **allocation stall** — a mutator thread blocking because no free memory is available and collection is not keeping pace — or an `OutOfMemoryError`. This gives a container a low steady-state footprint with headroom for spikes:

```
# Prefer roughly 4G in steady state, permitting growth to 6G under load
java -XX:+UseZGC -Xmx6g -XX:SoftMaxHeapSize=4g -jar app.jar
```

The flag is mutable at runtime:

```
jcmd <pid> VM.set_flag SoftMaxHeapSize 5g
```

Stalls are observable in the garbage-collection log rather than inferable from throughput graphs:

```
java -XX:+UseZGC -Xlog:gc,gc+stats=debug:file=gc.log:time,uptime -jar app.jar
```

Occurrences of `Allocation Stall` under normal load indicate the heap is undersized relative to allocation rate, or that `SoftMaxHeapSize` is set too low. The `gc` tag alone emits per-cycle pause and reclamation lines; `gc+stats`, emitted periodically at `debug` level, carries cumulative allocation-rate and stall counters suited to capacity planning.

## Pitfalls

- Passing `-XX:-ZGenerational` on JDK 24 or later does not select the old collector: JEP 490 made the option obsolete, so the JVM warns and ignores it rather than honouring it.
- Passing `-XX:-ZGenerational` on JDK 23 runs the deprecated non-generational collector with a warning, so a fleet-wide flag intended as a no-op silently pins some hosts to the mode JDK 24 removes.
- Reading pause-time lines alone hides the regression that matters here: pauses stay sub-millisecond while allocation stalls block mutator threads, so latency degrades with no change in reported pause duration.
- Setting `SoftMaxHeapSize` far below the live-set-plus-allocation-rate requirement produces continuous allocation stalls, because the ceiling is preferred rather than enforced only up to the point where stalling is the alternative.
- Treating generational mode as a tuning knob for pause time misreads the change: the pause guarantee is the same in both modes, and the difference is CPU and memory bandwidth consumed per unit of garbage reclaimed.
