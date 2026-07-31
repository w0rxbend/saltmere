---
title: "JEP 491: synchronized stops pinning your virtual threads (JDK 24)"
date: 2026-07-31
track: scala-jvm
summary: "Before JDK 24 the standing advice was: rewrite every synchronized block as a ReentrantLock or your virtual threads will pin their carriers and, worst case, deadlock the pool. JEP 491 makes that advice obsolete. Here's what changed under the hood and what you can stop doing."
reading_time: 5
tags: [jvm, virtual-threads, loom, concurrency, jdk24]
sources:
  - title: "JEP 491: Synchronize Virtual Threads without Pinning"
    url: "https://openjdk.org/jeps/491"
  - title: "Java 24 Stops Pinning Virtual Threads (Almost) — Inside Java Newscast #80"
    url: "https://inside.java/2024/11/21/newscast-80/"
  - title: "Java 24 — Thread pinning revisited (mikemybytes.com)"
    url: "https://mikemybytes.com/2025/04/09/java24-thread-pinning-revisited/"
---

Virtual threads (JDK 21) gave the JVM millions of cheap threads by *unmounting* a blocked virtual thread from its carrier platform thread, freeing that carrier to run someone else. The catch every early adopter hit: inside a `synchronized` block, a virtual thread **couldn't** unmount. It stayed welded — *pinned* — to its carrier for the whole time it blocked. Enough pinned carriers and your small pool of platform threads is exhausted, throughput collapses, and in the nasty case you deadlock waiting for a carrier that will never come free. JDK 24, released March 2025, fixes this with **JEP 491**.

## Why synchronized pinned in the first place

The old monitor implementation tracked ownership by the *platform* thread. When a virtual thread entered a `synchronized` method, the JVM recorded its carrier as the monitor's owner. If that virtual thread were allowed to unmount and a different virtual thread mounted on the same carrier, the second one would *appear* to own the monitor — breaking mutual exclusion outright. Rather than risk that, the JVM simply forbade unmounting while a monitor was held. Correct, but it turned every blocking call inside a lock into a pin.

That's why the JDK 21–23 playbook was "audit your code and your dependencies for `synchronized` around blocking I/O, and replace it with `java.util.concurrent.locks.ReentrantLock`," which never pinned because it doesn't use the monitor mechanism.

## What JEP 491 changed

The monitor implementation was reworked so a virtual thread can **acquire, hold, and release monitors independently of its carrier**. When a virtual thread blocks trying to enter a `synchronized` block (or calls `Object.wait()`), it now unmounts and releases the carrier like any other blocking point; when the monitor is available and the scheduler picks the thread, it remounts — possibly on a *different* carrier — and proceeds. Mutual exclusion is preserved because ownership now follows the virtual thread, not the carrier underneath it.

Concretely, this pins no more:

```java
private final Object lock = new Object();

void handle() {
    synchronized (lock) {          // JDK 23: pins the carrier while blocked inside
        var body = httpClient.send(req, ofString());   // blocking I/O
        repository.save(parse(body));                   // maybe blocking too
    }                              // JDK 24: virtual thread unmounts here, no pin
}
```

On JDK 24 you can run a few million virtual threads through that method and they'll happily share a handful of carriers. On 23 the same code throttles to roughly the carrier count.

## What you can stop doing

Two practical consequences. First, the mechanical `synchronized → ReentrantLock` migration is **no longer necessary just to avoid pinning.** Keep `synchronized` where it reads more clearly; reach for `ReentrantLock` only when you need its extra features (tryLock, fairness, multiple condition variables). Second, the diagnostics changed: the `-Djdk.tracePinnedThreads` flag was **removed** because synchronized is no longer a pinning source, and the `jdk.VirtualThreadPinned` JFR event now fires only for the remaining real cause — a virtual thread blocking while a **native frame** (a JNI call, or code inside a class initializer) is on its stack. So it's "almost" no pinning: native-boundary pins still exist and are rarer and harder to hit.

For Scala users on the JVM, the same win applies to any library still using `synchronized` internally — including a fair amount of older Java interop — without you rewriting it. The move is simply to run on JDK 24+ and delete the workarounds.

**Try next:** compile a method that does a blocking sleep inside `synchronized(lock)`, launch 10,000 virtual threads through it, and time the batch on JDK 23 vs JDK 24. Turn on `-Djdk.tracePinnedThreads=full` on 23 to watch the pins scroll by — then note the flag is gone (and the pins with it) when you switch the JDK.
