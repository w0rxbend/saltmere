---
title: "Stable Values: Lazy Fields the JVM Still Treats as Constants"
date: 2026-07-31
track: scala-jvm
summary: "JDK 25's preview StableValue API (JEP 502) gives you deferred, at-most-once immutability that the JIT can constant-fold like a final field, killing the double-checked-locking boilerplate."
reading_time: 5
tags: [jvm, jdk25, stable-values, concurrency, constant-folding, preview]
sources:
  - title: "JEP 502: Stable Values (Preview)"
    url: "https://openjdk.org/jeps/502"
  - title: "StableValue (Java SE 25 & JDK 25) API javadoc"
    url: "https://docs.oracle.com/en/java/javase/25/docs/api/java.base/java/lang/StableValue.html"
  - title: "JDK 25 project page (GA 16 Sept 2025, LTS)"
    url: "https://openjdk.org/projects/jdk/25/"
  - title: "Just Be Lazy — Inside.java"
    url: "https://inside.java/2025/07/29/just-be-lazy/"
---

Java's `final` gives you two guarantees the JIT loves: the field can't change, so the compiler can **constant-fold** reads of it straight into machine code. The price is timing — a `final` field must be assigned in the constructor or static initializer, i.e. **eagerly**. If the value is expensive (a parsed config, a heavy singleton, a logger), you pay for it at startup whether or not you use it.

The usual escape hatches all forfeit the optimization. Double-checked locking is error-prone boilerplate. The class-holder idiom (a nested `static` class whose init the classloader defers) works but only for statics and reads awkwardly. A `ConcurrentHashMap` cache is worst of all: the JVM can't trust a map entry to stay put, so **constant-folding is off the table**.

## What StableValue actually is

`StableValue<T>` (JEP 502, **preview** in JDK 25 — the LTS that reached GA on 16 September 2025) is a holder whose contents are set **at most once**. Under the hood it stores its value in a non-`final` field carrying the JDK-internal `@Stable` annotation — the same marker HotSpot uses for the constant pool. That's the trick: you get deferred initialization *and* the JIT is allowed to treat the value as a constant once it's observed, folding away every subsequent read from a `static final` holder.

So it's the class-holder idiom's constant-folding, but as a first-class object you can put in an instance field, a list, or a map.

## The core method: `orElseSet`

The workhorse is `orElseSet(Supplier)`: return the contents if set, otherwise compute them via the supplier, atomically, and cache. The supplier runs **at most once even under concurrent access** — competing threads block until the winner finishes, then observe its value. A successful write *happens-before* any read.

```java
// compile & run: --enable-preview --release 25
import java.util.function.Supplier;

public final class OrderService {

    // deferred, but JIT-foldable — no double-checked locking
    private static final StableValue<ExpensiveClient> CLIENT = StableValue.of();

    static ExpensiveClient client() {
        return CLIENT.orElseSet(() -> {
            // runs exactly once, lazily, thread-safely
            return ExpensiveClient.connect("db://prod");
        });
    }

    // even cleaner: a memoizing supplier, no explicit holder
    private static final Supplier<Logger> LOG =
        StableValue.supplier(() -> Logger.getLogger(OrderService.class));

    void submit(Order o) {
        LOG.get().info("submitting " + o.id());
        client().save(o);
    }
}
```

`StableValue.supplier(...)` hands back a caching `Supplier<T>` — the declaration site *is* the initialization logic, so there's no separate `getLogger()` boilerplate. The rest of the surface: `trySet`/`setOrThrow` for explicit one-shot writes, `orElse`/`orElseThrow`/`isSet` for reads. Note it's **not `Serializable`**, and `equals`/`hashCode` are identity-based.

## Stable collections

The same guarantee scales to whole structures, each element or entry computed at most once per key/index on first access:

```java
// lazily-filled, unmodifiable, constant-foldable per slot
static final List<Integer> POW2 = StableValue.list(32, v -> 1 << v);
static final Map<Level, Handler> HANDLERS =
    StableValue.map(Set.of(Level.INFO, Level.WARN), Level::buildHandler);
```

There are also `StableValue.intFunction(size, fn)` and `StableValue.function(keySet, fn)` for memoized functions over a bounded domain — a clean way to write self-referential memoized recursion (e.g. Fibonacci) with JIT-trusted caching.

## How it compares

- **`final` field:** eager, foldable. StableValue: **lazy**, foldable.
- **Double-checked locking / `volatile`:** manual, verbose, foldable only with care. StableValue: one method call, correct by construction.
- **Class-holder idiom:** statics only, foldable. StableValue: works in **instance** fields and collections too.
- **`@Stable`:** the internal annotation StableValue is built on. You can't use `@Stable` in application code — StableValue is its supported, safe surface, adding at-most-once atomicity on top of the raw folding contract.

Because it's a preview API, `StableValue` lives behind `--enable-preview --release 25` and its shape may shift before it's final. But the model — "compute once, then it's a constant" — is the point: you stop choosing between fast startup and fast steady state.

**Try next:** rewrite one double-checked-locking singleton in your codebase as a `StableValue.supplier(...)`, run it under `-Xlog:class+init` plus a JIT disassembler (`-XX:+PrintAssembly`) on a hot path, and confirm the post-warmup read has been folded to a constant.
