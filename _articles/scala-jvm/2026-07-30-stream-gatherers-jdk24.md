---
title: "Stream Gatherers: custom intermediate operations for Java Streams (JDK 24)"
date: 2026-07-30
track: scala-jvm
summary: "Java Streams exposed arbitrary terminal operations through Collector but no public way to define a lazy, stateful intermediate step. Gatherers, finalized in JDK 24 as JEP 485, supply that interface: the four-function model, the built-ins (windowSliding, fold, scan, mapConcurrent), and a custom distinct-until-changed gatherer."
reading_time: 6
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

**Gist.** The Java Stream API (application programming interface) admitted arbitrary user-defined *terminal* operations through `Collector`, but its *intermediate* operations were a closed set — `map`, `filter`, `flatMap`, `limit` — with no public extension point, so any operation carrying state across elements (collapse consecutive duplicates, overlapping windows) forced an exit from the pipeline into an imperative loop. **Gatherers**, finalized in **JDK 24** (March 2025) as **JEP 485** after two preview rounds, add `Stream.gather(Gatherer)` and a four-function model that mirrors `Collector` on the intermediate end. The cost is that the state is now the author's responsibility: a gatherer without a combiner **cannot participate in parallel evaluation**, so that stage runs sequentially even inside a parallel stream, and one with a combiner must supply a state merge that is correct under reordering.

## The four functions

A `Gatherer<T, A, R>` maps input elements of type `T` to output elements of type `R` while carrying private mutable state of type `A`. It is defined by up to four functions:

- **`initializer()`** → `Supplier<A>`: creates the private mutable **state** `A` carried between elements. Omitted for stateless gatherers.
- **`integrator()`** → `Integrator<A, T, R>`: the load-bearing function. For each input element it may inspect and update the state and **push** zero or more results downstream. It returns a `boolean`: `true` to continue, `false` to **short-circuit** the stream.
- **`combiner()`** → `BinaryOperator<A>`: merges two states so the operation can run in **parallel**. Omitted, the operation runs sequentially.
- **`finisher()`** → `BiConsumer<A, Downstream<R>>`: runs after the last element, allowing a buffering gatherer to flush its remaining state.

Two properties distinguish this from `map`. Gatherers can **short-circuit**: a `false` return from the integrator ends consumption of the input, so a gatherer composes with an infinite source and a downstream `limit` without consuming more elements than the pipeline needs. And gatherers are **stateful**: information legally crosses element boundaries in `A`, which `map` and `filter` cannot express.

The cardinality of the operation is not fixed by the interface. An integrator that pushes exactly once per element is one-to-one; pushing zero or many times per element yields filtering and expanding behaviour respectively; pushing only from the finisher yields a many-to-one reduction expressed as an intermediate step.

## The built-ins

`java.util.stream.Gatherers` ships several ready-made implementations:

```java
import java.util.stream.Gatherers;
import java.util.stream.Stream;
import java.util.List;
import java.util.Optional;

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

`windowSliding(3)` retains the last three elements in its state and emits on every element after the window fills; `fold` emits nothing until the finisher, which is why the result is read with `findFirst`.

**`mapConcurrent(maxConcurrency, mapper)`** is the operation with the largest behavioural surface. Each element is mapped on its **own virtual thread**, with at most `maxConcurrency` mappings in flight, and the output **preserves encounter order**. For input/output-bound mapping — issue N HTTP requests, keep at most 8 concurrent, receive results in the original order — this replaces explicit executor and future bookkeeping:

```java
List<Response> results = urls.stream()
    .gather(Gatherers.mapConcurrent(8, url -> httpClient.get(url)))  // ≤8 in flight
    .toList();
```

Order preservation and bounded concurrency are independent guarantees here: completion order is unconstrained, but the gatherer buffers so that emission follows encounter order.

## A custom gatherer: distinct-until-changed

Collapsing runs of consecutive equal elements is stateful (the last emitted value must be remembered) and lazy:

```java
import java.util.stream.Gatherer;

static final class Last<T> { T value; boolean seen; }   // private mutable state

static <T> Gatherer<T, ?, T> distinctUntilChanged() {
    return Gatherer.<T, Last<T>, T>ofSequential(
        Last::new,
        Gatherer.Integrator.ofGreedy((state, element, downstream) -> {
            if (!state.seen || !java.util.Objects.equals(state.value, element)) {
                state.seen = true;
                state.value = element;
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

Three details carry the semantics. `ofSequential` declares no combiner, so the operation runs in encounter order — required, because *consecutiveness* is not preserved once elements are split across parallel sub-streams. `Integrator.ofGreedy` declares an integrator that never short-circuits on its own initiative — it consumes every element it is offered, and returns `false` only to relay a refusal that came from downstream; `Integrator.of` is the form for an integrator that decides by itself to stop. And `downstream.push(...)` itself returns a `boolean` that is `false` when a **later** operation such as `limit` will accept no further elements; propagating that value upward is how early termination travels back along the pipeline.

The separate `seen` flag exists because `null` is a legal element: without it, a leading `null` would be indistinguishable from the initial state.

### Implementation sketch (Scala)

The same state machine expressed over Scala 3's `LazyList`, which is lazy in the same on-demand sense — the mechanism is the carried previous value plus a "nothing emitted yet" marker:

```scala
extension [T](xs: LazyList[T])
  def distinctUntilChanged: LazyList[T] =
    // `Option` plays the role of the `seen` flag above: `None` is
    // "nothing emitted yet", which no emitted value can collide with.
    def loop(rest: LazyList[T], last: Option[T]): LazyList[T] =
      rest match
        case h #:: tail =>
          if last.contains(h) then loop(tail, last)
          else h #:: loop(tail, Some(h))
        case _ => LazyList.empty
    loop(xs, None)

// The sliding window needs no hand-written state machine: the collections
// library already carries the retained window and emits once it is full.
val windows: LazyList[Seq[Int]] =
  LazyList(1, 2, 3, 4, 5).sliding(3).to(LazyList)

@main def demo(): Unit =
  println(LazyList(1, 1, 2, 2, 2, 3, 1).distinctUntilChanged.toList) // List(1, 2, 3, 1)
  println(windows.map(_.toList).toList) // List(List(1, 2, 3), List(2, 3, 4), List(3, 4, 5))
```

`h #:: loop(...)` keeps the tail unevaluated, which is the counterpart of a gatherer pulling elements on demand: the recursion advances only as far as the consumer demands.

## Position in the API

Before Gatherers, each of these operations required abandoning the declarative pipeline for a loop, losing laziness and composability. A `Gatherer` is implemented once and reused: it applies to any stream, composes with `andThen`, and — when a combiner is supplied — participates in parallel execution. This extends the operator set available in core Java without an external dependency.

## Pitfalls

- **A combiner supplied for an order-dependent gatherer silently produces wrong output.** Under parallel execution the stream is split into sub-streams whose states are merged pairwise; an operation whose meaning depends on adjacency (distinct-until-changed, sliding windows) has no correct merge across a split boundary. `Gatherer.ofSequential` is the safe declaration.
- **`Integrator.ofGreedy` asserts the integrator never stops the stream on its own.** An integrator that decides for itself to stop early — after seeing a sentinel element, say — must be declared with `Integrator.of`, not `ofGreedy`; relaying a `false` that came from `downstream.push` is the one case a greedy integrator may return `false`.
- **Discarding the value returned by `downstream.push(...)` disables downstream early termination.** Returning `true` unconditionally means a subsequent `limit` cannot signal that it wants no further elements, so an infinite source keeps being consumed.
- **A buffering gatherer without a finisher loses its tail.** State held at end of input — a partially filled window, a pending fold accumulator — is never emitted unless the finisher flushes it.
- **`fold` emits nothing until the input is exhausted.** Placing it in a pipeline over an unbounded source produces no elements and does not terminate.
- **`mapConcurrent` bounds in-flight mappings, not the total thread count of the process.** Multiple concurrent pipelines each honour their own `maxConcurrency` independently.
