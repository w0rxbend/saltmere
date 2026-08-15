---
title: "What's new in Scala 3.8: better fors, runtimeChecked, and the optimizer returns"
date: 2026-08-13
track: scala-jvm
summary: "The Scala 3.8 line (current: 3.8.4, June 2026) raises the floor to JDK 17, turns on SIP-62 improved for-comprehensions by default, replaces ': @unchecked' with runtimeChecked, and — as of 3.8.3 — ports the Scala 2 JVM bytecode optimizer with its -opt inliner. What each change does and what it costs."
reading_time: 6
tags: [scala-3-8, sip-62, runtimechecked, optimizer, migration]
sources:
  - title: "Scala 3.8 released! — scala-lang.org"
    url: "https://www.scala-lang.org/news/3.8/"
  - title: "Scala 3.8.3 is now available! — scala-lang.org"
    url: "https://www.scala-lang.org/news/3.8.3/"
  - title: "Scala 3.8.4 is now available! — scala-lang.org"
    url: "https://www.scala-lang.org/news/3.8.4/"
  - title: "Scala release lines — endoflife.date"
    url: "https://endoflife.date/scala"
---

**Gist.** Scala 3.8 is the feature set that freezes into the next long-term-support (LTS) line — Scala 3.9 is described as 3.8's features, stabilized — so its changes are not transitional. The mechanism of the change is a coordinated platform move: **a Java Development Kit (JDK) 17 floor** for both compiling and running, a new default desugaring for `for`-comprehensions (SIP-62, where SIP is a Scala Improvement Proposal), an expression-level replacement for `: @unchecked`, and the Scala 2 bytecode optimizer ported into the Scala 3 backend as of 3.8.3. The cost is a hard break in deployment targets: only the 3.3 LTS line still emits JDK 8-compatible bytecode, and code built with the inliner enabled becomes coupled to the exact bytecode of everything it inlines from.

Scala 3.8.0 opened the line in early 2026; 3.8.4 (June 2026) is the current stable release. **3.8.0 and 3.8.1 should be skipped**: both carried regressions fixed later in the line.

## The JDK 17 floor

Scala 3.8 requires JDK 17 or later for compilation and for running compiled code. The documented driver is JDK 26 and later locking down `sun.misc.Unsafe`; **the compiler's lazy-value implementation now uses `VarHandle`s instead of `Unsafe`**. A `VarHandle` is a typed reference to a field that exposes atomic and ordered accesses, so the lazy-val initialization protocol keeps its publication guarantees without the removed API. The practical consequence is binary, not gradual: a continuous-integration image pinned to JDK 11 stops compiling entirely rather than degrading.

## Improved for-comprehensions on by default (SIP-62)

Previously gated behind `experimental.betterFors`, the improved desugaring is now the default meaning of `for`. Two changes are observable.

First, **an alias may appear before any generator**, which was previously a syntax error:

```scala
for
  base = config.retryBase          // alias first — was a syntax error
  attempt <- 1 to maxRetries
  delay = base * math.pow(2, attempt)
yield schedule(delay)
```

Second, **a trailing alias or a trivial `yield x` no longer emits an extra `.map`**. Under the old desugaring, a comprehension ending in an identity map produced one additional `map` call on the underlying type. For an effect type such as those in ZIO or Cats Effect, `map` is not free: it allocates a node in the effect's instruction tree and a closure to hold the function. Removing the identity map removes both per comprehension that had one. No flag enables this; recompiling is sufficient.

The invariant worth stating explicitly: **the desugaring changes which method calls are emitted, not what the comprehension means for lawful types**. Where a type's `map` has an observable side effect at identity — a logging wrapper, an instrumented collection — the removed call is an observable difference.

## runtimeChecked replaces `: @unchecked` (SIP-57)

The prior way to suppress a non-exhaustive-match warning was the type ascription `: @unchecked`, which attaches to a type position and is easy to misplace. Scala 3.8 stabilizes an extension method that carries the same instruction at the expression level:

```scala
val port = sys.env.get("PORT").runtimeChecked match
  case Some(p) => p.toInt          // no non-exhaustive warning

// destructuring that previously required (xs: @unchecked)
val head :: tail = readList().runtimeChecked
```

**The runtime semantics are unchanged: a failed match still throws `MatchError`.** What changes is auditability — each `runtimeChecked` is a grep-addressable site where runtime checking was chosen over a static exhaustiveness proof.

### Implementation sketch (Scala)

The two features compose in the place they most often appear together: parsing a partially-trusted configuration map where the shape is known but not provable to the compiler.

```scala
final case class RetrySpec(base: Double, attempts: Int, delays: Vector[Double])

def parse(env: Map[String, String]): Option[RetrySpec] =
  for
    // alias before any generator — legal under SIP-62
    exponent  = 2.0
    baseStr  <- env.get("RETRY_BASE")
    countStr <- env.get("RETRY_ATTEMPTS")
    base      = baseStr.toDouble
    attempts  = countStr.toInt
    delays    = (1 to attempts).map(n => base * math.pow(exponent, n)).toVector
  yield RetrySpec(base, attempts, delays)
  // trailing aliases + yield: no identity `.map` is emitted

def head3(spec: RetrySpec): (Double, Double, Double) =
  // the compiler cannot prove three elements exist; the assertion is explicit
  val a +: b +: c +: _ = spec.delays.runtimeChecked
  (a, b, c)                       // MatchError if fewer than three delays
```

`head3` states the failure mode in the code rather than in a comment: fewer than three delays produces a `MatchError` at that binding, not a silent wrong answer downstream.

## The Scala 2 optimizer, ported (3.8.3 and later)

Scala 3.8.3 ports the Scala 2 JVM backend optimizer to Scala 3. `-opt` enables bytecode optimizations; `-opt-inline` scopes the inliner with patterns: `**` for everything on the classpath, `a.**` for a package subtree, `<sources>` for the sources being compiled, and a `!` prefix for exclusions.

```scala
// build.sbt — inline within the project's own packages only
scalacOptions ++= Seq("-opt", "-opt-inline:myapp.**", "-Wopt")
```

`-Wopt` surfaces inliner warnings, such as a method annotated `@inline` that could not be inlined. `-Yopt-log-inline` and `-Yopt-trace` report individual decisions.

The Scala 2 rule carries over unchanged and is the load-bearing constraint: **inlined bytecode bakes in the callee's internals, so inlining across the boundary of a library that is not recompiled together with the caller is unsound under upgrade**. The failure is delayed and unhelpful — the caller keeps the old inlined body while the library's own classes change, and the mismatch surfaces as a `NoSuchMethodError`, a `LinkageError`, or stale behaviour, at a call site whose source shows nothing wrong. The gains this optimizer produced on Scala 2 were workload-dependent and are not quantified here; no published Scala 3 benchmark is cited alongside the port, so the payoff has to be measured per build rather than assumed. Since 3.8.4, `:help` may be appended to a compiler flag — `scalac "-opt-inline:help"` — to print its pattern syntax.

## Preview and experimental tiers

Under `-preview`, described as on track to become default: **SIP-71 into conversions**, letting a library author mark a parameter type as `into[T]` so callers receive implicit conversions without importing `implicitConversions`; and **SIP-75 relaxed lambda syntax**, permitting a single-line lambda after a colon — `xs.map: v => v + 1`.

Behind `experimental`: SIP-70 flexible varargs (`sum(0, a*, b*, 5)`), match sub-cases, and — new in 3.8.3 — **safe mode** (`import language.experimental.safe`), a language subset that rejects unchecked casts and escape hatches. The tiering is itself the contract: preview features are one release from default, experimental ones may still change.

One structural change sits underneath: **the standard library is now compiled with Scala 3 rather than inherited as Scala 2 bytecode**. This unblocks stdlib evolution — a Scala 3-compiled standard library can carry Scala 3-only metadata, which a Scala 2 artifact cannot — and means TASTy (the typed abstract syntax tree serialization format) covers the whole platform.

## Migrating

Three items break builds in practice. JDK 17 is the floor, so images on 11 stop compiling. **The read-eval-print loop (REPL) is now a separate `scala3-repl` artifact**, so tooling that embedded it needs a new dependency. And standard-library context bounds now desugar to `given` rather than `implicit`, changing the shape of rare explicit calls: `Array.empty(using reflect.ClassTag.Int)`.

Minimum build-tool versions: sbt 1.11.5 (1.12 or later recommended), Mill 1.0.5, Scala CLI 1.9.0. Release 3.8.4 additionally hardened TASTy parsing against maliciously crafted files, which is relevant where tooling reads TASTy from untrusted artifacts.

## Pitfalls

- **Adopting 3.8.0 or 3.8.1.** Both lines carry runtime regressions fixed later; the symptom is a failure that disappears on 3.8.4 with no source change.
- **A JDK 11 continuous-integration image.** Compilation fails outright rather than warning, because the JDK 17 floor applies to the compiler itself, not only to the target bytecode version.
- **Classpath-wide inlining on a build with external dependencies.** Callee internals are copied into the caller's bytecode; a later dependency upgrade without recompiling the caller yields `NoSuchMethodError`, `LinkageError`, or stale behaviour at a call site whose source is unchanged.
- **Expecting `runtimeChecked` to add a check.** It removes a compile-time warning and changes nothing at runtime; a non-matching value still throws `MatchError` at the match or binding site.
- **Relying on an identity `map` for effects.** Under SIP-62 a comprehension ending in a trivial `yield x` no longer emits that `map`, so a wrapper type whose `map` logs or counts records one fewer call after recompiling.
- **Targeting JDK 8 from 3.8.** Only the 3.3 LTS line produces JDK 8-compatible bytecode; a 3.8 build cannot be made to emit it by lowering a release flag.
- **Assuming preview equals stable.** `-preview` features such as SIP-71 and SIP-75 are documented as on track for default, not as frozen, and experimental features including safe mode may still change shape.
