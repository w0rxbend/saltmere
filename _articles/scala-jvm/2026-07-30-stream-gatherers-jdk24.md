---
title: "Stream Gatherers: the custom intermediate operation Streams always lacked (JDK 24)"
date: 2026-07-30
track: scala-jvm
summary: "Java Streams had rich terminal operations via Collector but no way to write your own lazy, stateful intermediate step — until Gatherers, finalized in JDK 24 as JEP 485. Here's the four-part Gatherer model, the built-ins (windowSliding, fold, scan, mapConcurrent), and a custom distinct-until-changed gatherer."
reading_time: 5
tags: [java, streams, gatherers, jep-485, jdk24, functional]
sources:
  - title: "JEP 485: Stream Gatherers — OpenJDK"
    url: "https://openjdk.org/jeps/485"
  - title: "Stream Gatherers — Java Core Libraries Guide (JDK 24)"
    url: "https://docs.oracle.com/en/java/javase/24/core/stream-gatherers.html"
  - title: "java.util.stream.Gatherer — JDK 24 API documentation"
    url: "https://docs.oracle.com/en/java/javase/24/docs/api/java.base/java/util/stream/Gatherer.html"
  - title: "Stream Gatherers in JDK 24 — Dan Vega"
    url: "https://www.danvega.dev/blog/stream-gatherers"
---

The Stream API has always been lopsided. On the terminal end you have `Collector`, a rich, composable interface for building *any* reduction you like. But the intermediate operations — `map`, `filter`, `flatMap`, `limit` — are a fixed set the JDK hands you, and there was no public way to write your own lazy, stateful one. Want "emit an item only when it differs from the previous"? Or "group into overlapping windows of three"? You'd fall out of the stream into an imperative loop. **Gatherers**, finalized in **JDK 24** (March 2025) as **JEP 485** after two preview rounds, close that gap: `Gatherer` is to intermediate operations what `Collector` is to terminal ones.

## The shape: `stream.gather(...)`

A gatherer plugs into a new intermediate method, `Stream.gather(Gatherer)`, and — like `Collector` — is defined by up to four functions:

- **`initializer()`** → `Supplier<A>`: creates the private mutable **state** (`A`) the operation carries between elements. Omit it for stateless gatherers.
- **`integrator()`** → `Integrator<A, T, R>`: the heart. For each input element `T`, it may inspect/update state and **push** zero or more results `R` downstream. It returns a `boolean`: `true` to keep going, `false` to **short-circuit** the whole stream.
- **`combiner()`** → `BinaryOperator<A>`: merges two states so the gatherer can run in **parallel**. Omit it and the operation runs sequentially.
- **`finisher()`** → `BiConsumer<A, Downstream<R>>`: runs after the last element, so a gatherer that buffers can flush what's left.

The two properties that make this more than a fancy `map`: gatherers are **lazy** (they pull elements on demand and can stop early via the `false` return, so they compose with infinite streams and `limit`) and **stateful** (the `A` state legally carries information *across* elements, which plain `map`/`filter` cannot).

## Start with the built-ins

`java.util.stream.Gatherers` ships several ready-made ones that were previously awkward or impossible:

```java
import java.util.stream.Gatherers;
import java.util.stream.Stream;
import java.util.List;

// windowFixed / windowSliding — group elements into lists
List<List<Integer>> sliding = Stream.of(1, 2, 3, 4, 5)
    .gather(Gatherers.windowSliding(3))
    .toList();
// [[1,2,3], [2,3,4], [3,4,5]]

// scan — running accumulation, emitting each intermediate (one-to-one)
List<Integer> runningSum = Stream.of(1, 2, 3, 4)
    .gather(Gatherers.scan(() -> 0, Integer::sum))
    .toList();
// [1, 3, 6, 10]

// fold — a strict many-to-one reduction, as an intermediate op
Optional<String> joined = Stream.of("a", "b", "c")
    .gather(Gatherers.fold(() -> "", (acc, x) -> acc + x))
    .findFirst();
// "abc"
```

The standout is **`mapConcurrent(maxConcurrency, mapper)`**: it maps each element on its **own virtual thread** (see the virtual-threads article here), capped at `maxConcurrency` in flight, while **preserving encounter order** in the output. For I/O-bound mapping — fan out N HTTP calls, keep at most 8 concurrent, get results back in order — this is the whole boilerplate of an executor-plus-futures collapsed into one line:

```java
List<Response> results = urls.stream()
    .gather(Gatherers.mapConcurrent(8, url -> httpClient.get(url)))  // ≤8 in flight
    .toList();
```

## Write your own: distinct-until-changed

The classic "collapse consecutive duplicates" operation — trivial in reactive libraries, absent from core Streams — is a few lines as a gatherer. It's stateful (remembers the last emitted value) and lazy:

```java
import java.util.stream.Gatherer;

static <T> Gatherer<T, ?, T> distinctUntilChanged() {
    return Gatherer.ofSequential(
        () -> new Object() { T last = null; boolean seen = false; },   // state
        Gatherer.Integrator.ofGreedy((state, element, downstream) -> {
            if (!state.seen || !java.util.Objects.equals(state.last, element)) {
                state.seen = true;
                state.last = element;
                return downstream.push(element);   // emit only on change
            }
            return true;                            // duplicate: consume, emit nothing
        })
    );
}

// usage
List<Integer> out = Stream.of(1, 1, 2, 2, 2, 3, 1)
    .gather(distinctUntilChanged())
    .toList();
// [1, 2, 3, 1]
```

`ofSequential` says "no combiner, run in order" — correct here, since consecutiveness is meaningless once elements are reordered by parallelism. `Integrator.ofGreedy` marks an integrator that never short-circuits (it always returns `true`), which lets the runtime optimize; use the plain `Integrator.of` when you *do* want to return `false` to stop early. And `downstream.push(...)` itself returns a `boolean` — `false` when a *later* operation (like `limit`) no longer wants elements — which is how backpressure and early termination flow back up the pipeline.

## Why it matters beyond convenience

Before Gatherers, any of these operations forced you to abandon the declarative pipeline for a loop, losing laziness and composability. Now the pattern is: reach for a built-in gatherer first, and when your logic is genuinely custom, implement `Gatherer` once and reuse it everywhere — it slots into any stream, composes with `andThen`, and (with a combiner) parallelizes. It's the missing half of the Stream API, and it makes Java streams competitive with the richer operator sets of reactive libraries for straightforward stateful transforms — without pulling in a dependency.

**Try next:** Write a `rateLimited(perSecond)` gatherer that carries a timestamp in its state and sleeps between pushes, then chain it after `mapConcurrent` to fan out and then throttle downstream consumption — a two-gatherer pipeline that would otherwise be a page of executor and clock bookkeeping.
