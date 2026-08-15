---
title: "Stable Values: Lazy Fields the JVM Still Treats as Constants"
date: 2026-07-31
track: scala-jvm
summary: "JDK 25's preview StableValue API (JEP 502) provides deferred, at-most-once immutability that the JIT compiler can constant-fold like a final field, removing double-checked-locking boilerplate."
reading_time: 6
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

**Gist.** A `final` field on the Java Virtual Machine (JVM) can be constant-folded by the JIT compiler, which translates bytecode to machine code during execution, because it cannot change, but it must be assigned eagerly in a constructor or static initializer, so an expensive value is paid for at startup whether or not it is used. `StableValue<T>` (JEP 502, preview in JDK 25) is a holder set **at most once**, backed by a field carrying the JDK-internal `@Stable` annotation, so initialization is deferred while the JIT is still permitted to treat the observed value as a constant. The cost is a preview API whose shape may change, a holder that is not `Serializable`, identity-based `equals`/`hashCode`, and initialization that is unordered in time rather than pinned to class initialization.

## The constraint being escaped

`final` couples two properties that are separable in principle: **immutability after publication** and **assignment before publication**. The JIT depends on the first; the startup cost comes from the second. Every conventional workaround relaxes assignment timing and loses the optimization along the way.

- **Double-checked locking** with a `volatile` field is manual boilerplate whose correctness depends on the fence placement, and the `volatile` read is **not constant-folded** — the JIT must reload it.
- **The class-holder idiom** — a nested `static` class whose initialization the classloader defers to first use — is foldable, but it applies to **statics only** and requires a separate holder type per value.
- **A `ConcurrentHashMap` cache** is the weakest case: the JVM cannot assume a map entry stays put, so **constant-folding is unavailable** for values reached through it.

## The mechanism

`StableValue<T>` stores its contents in a non-`final` field annotated `@Stable`, the same marker HotSpot uses to license constant-folding of a non-`final` field. The contract that annotation carries is one-directional: the field may transition **once** from its default value to a non-default value, and once the JIT has observed a non-default value it may compile that value in as a constant. A read from a `static final` holder that has already been set therefore folds away entirely in the compiled code.

The state machine has two states — unset and set — and **no edge back to unset**. `StableValue` adds at-most-once atomicity on top of the raw folding contract, which is what makes the annotation safe to expose: `@Stable` itself is not usable from application code, because code that violates the one-way transition has undefined behaviour rather than merely a stale read.

The consequence worth stating precisely: this is the class-holder idiom's constant-folding available as an ordinary object, so it can live in an **instance** field, a list, or a map.

## `orElseSet` and the happens-before edge

`orElseSet(Supplier)` returns the contents if set, otherwise computes them via the supplier, atomically, and caches the result. The supplier runs **at most once even under concurrent access**: competing threads block until the winning thread finishes, then observe its value. **A successful write happens-before any subsequent read**, so the initialized object's own fields are safely published without additional synchronization.

```java
// compile & run: --enable-preview --release 25
import java.util.function.Supplier;

public final class OrderService {

    // deferred, but JIT-foldable — no double-checked locking
    private static final StableValue<ExpensiveClient> CLIENT = StableValue.of();

    static ExpensiveClient client() {
        return CLIENT.orElseSet(() -> {
            // at most one *successful* run, however many threads arrive
            return ExpensiveClient.connect("db://prod");
        });
    }

    // a memoizing supplier, no explicit holder
    private static final Supplier<Logger> LOG =
        StableValue.supplier(() -> Logger.getLogger(OrderService.class.getName()));

    void submit(Order o) {
        LOG.get().info("submitting " + o.id());
        client().save(o);
    }
}
```

`StableValue.supplier(...)` returns a caching `Supplier<T>` in which the declaration site is the initialization logic, so no separate accessor method is required. The remaining surface is small: `trySet`/`setOrThrow` for explicit one-shot writes, and `orElse`/`orElseThrow`/`isSet` for reads. The holder is **not `Serializable`**, and `equals`/`hashCode` are identity-based.

## Stable collections

The at-most-once guarantee extends to whole structures, with each element or entry computed **at most once per index or key**, on first access:

```java
// lazily-filled, unmodifiable, constant-foldable per slot
static final List<Integer> POW2 = StableValue.list(32, v -> 1 << v);
static final Map<String, Charset> CHARSETS =
    StableValue.map(Set.of("UTF-8", "ISO-8859-1"), Charset::forName);
```

`StableValue.intFunction(size, fn)` and `StableValue.function(keySet, fn)` provide memoized functions over a **bounded domain** — the index range or the key set is fixed at construction — which permits self-referential memoized recursion, such as Fibonacci, with caching the JIT is allowed to trust.

### Implementation sketch (Scala)

Scala's `lazy val` already occupies this niche on the language side; the sketch below makes the at-most-once invariant explicit rather than delegating to the compiler, which is the shape `orElseSet` implements.

```scala
final class AtMostOnce[A]:
  // null encodes "unset"; the field only ever moves null -> value
  @volatile private var value: AnyRef | Null = null
  private val lock = new Object

  def orElseSet(compute: () => A): A =
    val seen = value
    if seen != null then seen.asInstanceOf[A]           // fast path: no lock
    else
      lock.synchronized:
        if value == null then value = compute().asInstanceOf[AnyRef]
        value.asInstanceOf[A]

  def isSet: Boolean = value != null

// memoization over a bounded domain, as StableValue.intFunction does
final class BoundedMemo[A](size: Int, fn: Int => A):
  private val slots = Array.fill(size)(new AtMostOnce[A])

  def apply(i: Int): A =
    require(i >= 0 && i < size, s"index $i outside domain 0..${size - 1}")
    slots(i).orElseSet(() => fn(i))   // fn may re-enter apply on a *different* i
```

Two properties carry the weight. The **fast path reads the field without acquiring the lock**; the lock exists only to make the transition single-winner. The sketch does not reproduce the constant-folding — a `volatile` read is reloaded, and `@Stable` is unavailable to application code — which is exactly the gap `StableValue` exists to close. And **`fn` may re-enter `apply`**, which is what makes self-referential recursion work, provided the recursion is ordered so that it terminates: `synchronized` is reentrant, so a cycle does not deadlock but recomputes without bound until the stack is exhausted.

## Comparison

- **`final` field:** eager, foldable. `StableValue`: **lazy**, foldable.
- **Double-checked locking with `volatile`:** manual, verbose, and the guarded read is not folded. `StableValue`: one method call.
- **Class-holder idiom:** statics only, foldable. `StableValue`: also usable in **instance** fields and collections.
- **`@Stable`:** the JDK-internal annotation `StableValue` is built on, unavailable to application code. `StableValue` is its supported surface, adding at-most-once atomicity to the folding contract.

Because it is a preview application programming interface (API), `StableValue` requires `--enable-preview --release 25` and its shape may change before it is final. JDK 25 reached general availability on 16 September 2025 as a long-term-support release.

## Pitfalls

- **Compiling without `--enable-preview --release 25` fails outright**, and a class compiled with preview features refuses to load on a runtime started without `--enable-preview` — including any downstream consumer of the artifact.
- **A supplier that throws leaves the holder unset**, so the next call retries the computation; an initializer with side effects can therefore run more than once even though a *successful* initialization runs at most once.
- **A supplier that re-enters `orElseSet` on the same holder cannot make progress**, because the value it needs is the one it is currently computing. Memoized recursion is safe only across distinct holders, ordered so recursion terminates.
- **`setOrThrow` on an already-set holder throws**, so it is unsuitable wherever more than one code path may initialize the value; `trySet` reports the loss instead.
- **The domain of `StableValue.map`, `list`, `function` and `intFunction` is fixed at construction.** Keys outside the supplied key set and indices outside the declared size are not computed lazily; they are not members of the structure at all.
- **Identity-based `equals`/`hashCode` mean two holders with equal contents are unequal**, so placing holders in a `HashSet` or using them as map keys compares boxes rather than values.
- **The holder is not `Serializable`**, so a class that serializes cleanly today stops doing so when a field is converted from an eagerly computed value to a `StableValue`.
- **Deferring work does not remove it.** The first request through a lazily initialized path pays the full initialization cost, moving a startup expense into tail latency rather than eliminating it.
