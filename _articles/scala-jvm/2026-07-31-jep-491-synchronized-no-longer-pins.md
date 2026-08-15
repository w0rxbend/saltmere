---
title: "JEP 491: synchronized no longer pins virtual threads (JDK 24)"
date: 2026-07-31
track: scala-jvm
summary: "Before JDK 24, a virtual thread that blocked inside a synchronized block stayed welded to its carrier, capping concurrency at the carrier count and risking deadlock. JEP 491 moves monitor ownership from the carrier to the virtual thread, which removes that pin and retires the associated diagnostics."
reading_time: 6
tags: [jvm, virtual-threads, loom, concurrency, jdk24]
sources:
  - title: "JEP 491: Synchronize Virtual Threads without Pinning"
    url: "https://openjdk.org/jeps/491"
  - title: "Java 24 Stops Pinning Virtual Threads (Almost) — Inside Java Newscast #80"
    url: "https://inside.java/2024/11/21/newscast-80/"
  - title: "Java 24 — Thread pinning revisited (mikemybytes.com)"
    url: "https://mikemybytes.com/2025/04/09/java24-thread-pinning-revisited/"
---

**Gist.** Virtual threads (JDK 21) scale because a blocked virtual thread *unmounts* from its carrier — a platform thread drawn from a small pool — leaving the carrier free to run another virtual thread. Until JDK 24 that unmounting was forbidden while a monitor was held, so any blocking call inside a `synchronized` block *pinned* the carrier for the duration, capping effective concurrency at the number of carriers and, in the worst case, deadlocking when no carrier could be freed. **JEP 491**, delivered in JDK 24, reimplements monitors so ownership is tracked per virtual thread rather than per carrier; the cost is a reworked monitor implementation whose diagnostics changed, and a residual class of pins at native frames that remains.

## The ownership invariant that forced the pin

The invariant a monitor must maintain is that **at most one thread of execution holds the monitor, and only that same thread may release it**. The pre-JEP-491 implementation identified the holder by the *platform* thread. When a virtual thread entered a `synchronized` method, the carrier underneath it was recorded as the owner.

That identification is what made unmounting unsafe. If the virtual thread unmounted while holding the monitor, and a second virtual thread mounted on the same carrier, the second one would present the same platform-thread identity and therefore **appear to own a monitor it never entered** — mutual exclusion violated, not merely delayed. The implementation avoided that by forbidding unmounting whenever a monitor was held.

The consequence is a scheduling failure mode rather than a correctness one. With *N* carriers, at most *N* virtual threads can be inside a blocking region guarded by `synchronized` at once; every other runnable virtual thread waits, whatever its own work would have been. If the pinned threads are blocked on something that can only be completed by work requiring a carrier, the pool cannot make progress at all.

This is why the JDK 21–23 guidance was to audit application and dependency code for `synchronized` wrapped around blocking input/output and replace it with `java.util.concurrent.locks.ReentrantLock`, which is built on the virtual-thread-aware parking machinery rather than on monitors and so did not pin.

## What JEP 491 changes

The monitor implementation was reworked so that a virtual thread can **acquire, hold and release monitors independently of its carrier**. The blocking points behave like every other virtual-thread blocking point:

- On contended entry to a `synchronized` block or method, the virtual thread **unmounts and releases its carrier** instead of blocking the carrier.
- On `Object.wait()`, likewise: the thread unmounts while waiting for the notification.
- When the monitor becomes available and the scheduler selects the thread, it **remounts, possibly on a different carrier**, and continues.

Mutual exclusion survives because ownership now follows the virtual thread across mount points, so the identity recorded at entry is the identity checked at exit — regardless of which carrier executes either.

```java
private final Object lock = new Object();

void handle() {
    synchronized (lock) {          // JDK 23: carrier pinned while blocked inside
        var body = httpClient.send(req, ofString());   // blocking I/O
        repository.save(parse(body));                   // possibly blocking
    }                              // JDK 24: virtual thread unmounts here, no pin
}
```

Under JDK 23, the throughput of this method is bounded by the carrier count, because each in-flight call occupies a carrier for the full duration of the request. Under JDK 24, in-flight calls are bounded instead by the monitor's own serialisation: they queue on `lock` without consuming carriers.

Note the distinction that remains: **`synchronized` still serialises**. JEP 491 removes the carrier pin, not the mutual exclusion. A hot lock around a slow call is still a throughput ceiling — it is now a ceiling caused by the critical section rather than by the scheduler's thread supply.

## What the change retires

Two consequences follow for existing code and tooling.

The mechanical `synchronized` → `ReentrantLock` migration is **no longer required to avoid pinning**. `ReentrantLock` remains the choice where its additional operations are needed — `tryLock`, optional fairness, several condition variables per lock — but not as a workaround.

The diagnostics changed. The `-Djdk.tracePinnedThreads` flag was **removed**, since `synchronized` is no longer a pinning source. The `jdk.VirtualThreadPinned` Java Flight Recorder (JFR) event still fires, for the remaining cause: a virtual thread blocking while a **native frame is on its stack** — a native method, or a call through the Foreign Function and Memory (FFM) API. Hence "almost" no pinning: mikemybytes reports these cases as unlikely to affect most applications, but they are not eliminated.

For Scala on the Java Virtual Machine (JVM), the effect reaches any library that uses `synchronized` internally, including Java interop code that cannot practically be rewritten, without changes at the call site. Running on JDK 24 or later is the whole migration.

### Implementation sketch (Scala)

The observable difference is a scheduling one, so it can be measured directly: run *n* virtual threads through a `synchronized` region that blocks, and compare wall-clock time across JDK versions.

```scala
object PinProbe:

  // One monitor per task: uncontended, so any serialisation observed comes
  // from carrier supply rather than from mutual exclusion.
  private def guardedBlockingCall(lock: Object, millis: Long): Unit =
    lock.synchronized:
      Thread.sleep(millis)

  private def unguardedBlockingCall(millis: Long): Unit =
    Thread.sleep(millis)

  private def timeMillis(threads: Int)(body: Int => Unit): Long =
    val start = System.nanoTime()
    val ts: Vector[Thread] =
      Vector.tabulate(threads): i =>
        Thread.ofVirtual().start(() => body(i))
    ts.foreach(_.join())
    (System.nanoTime() - start) / 1_000_000

  @main def main(): Unit =
    val n = 10_000
    val millis = 50L
    val locks = Vector.fill(n)(new Object)

    println(s"guarded:   ${timeMillis(n)(i => guardedBlockingCall(locks(i), millis))} ms")
    println(s"unguarded: ${timeMillis(n)(_ => unguardedBlockingCall(millis))} ms")
```

Both variants should complete on the order of `millis` on JDK 24, since neither the monitors nor the sleeps hold a carrier. On JDK 23 only the unguarded variant does; the guarded one degrades towards `n / carriers * millis`. Enabling `-Djdk.tracePinnedThreads=full` on JDK 23 reports the pins as they occur; on JDK 24 the flag no longer exists.

## Pitfalls

- **Expecting `synchronized` to stop being a bottleneck.** JEP 491 removes the carrier pin, not the mutual exclusion; a single lock around a 50 ms call still admits one thread at a time, so throughput stays bounded by the critical section.
- **Assuming all pinning is gone.** A virtual thread that blocks with a native frame on its stack — a native method, or a foreign function called through the FFM API — continues to pin its carrier, and the `jdk.VirtualThreadPinned` JFR event still reports it.
- **Leaving `-Djdk.tracePinnedThreads` in a launch script.** The flag was removed in JDK 24; startup arguments carried over from a JDK 21–23 configuration reference a property that no longer has any effect.
- **Benchmarking with one shared lock.** A microbenchmark whose tasks contend on a single monitor is serialised by the monitor itself and shows no difference between JDK 23 and JDK 24, which invites the conclusion that the change did nothing.
- **Interpreting a remaining throughput cliff as a pin.** After JDK 24 the same symptom — concurrency capped near the carrier count — can come from blocking calls that are not virtual-thread-aware at all, and the removed tracing flag no longer helps to distinguish them.
