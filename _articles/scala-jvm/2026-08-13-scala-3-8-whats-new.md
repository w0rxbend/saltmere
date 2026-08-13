---
title: "What's new in Scala 3.8: better fors, runtimeChecked, and the optimizer returns"
date: 2026-08-13
track: scala-jvm
summary: "The Scala 3.8 line (current: 3.8.4, June 2026) raises the floor to JDK 17, turns on SIP-62 improved for-comprehensions by default, replaces ': @unchecked' with runtimeChecked, and — as of 3.8.3 — ports the Scala 2 JVM bytecode optimizer with its -opt inliner. What changed and how to use each piece."
reading_time: 5
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

Scala 3.8.0 shipped on January 22, 2026, and the line has settled: 3.8.4 (June 5, 2026) is the current stable release, and it's also the feature set that will freeze into the next LTS — Scala 3.9 will be 3.8's features, stabilized. That makes 3.8 worth understanding now: it's not a waypoint, it's the shape of the platform for the next few years. Skip 3.8.0 and 3.8.1 entirely; both had runtime regressions fixed later in the line.

The loudest change is operational: **Scala 3.8 requires JDK 17 or later**, for compilation and for running compiled code. Only the 3.3 LTS keeps producing JDK 8-compatible bytecode. The driver is JDK 26+ locking down `sun.misc.Unsafe` — the compiler's lazy vals now use `VarHandles` instead — so the platform moves forward together or not at all.

## Better fors are on by default (SIP-62)

Previously hidden behind `experimental.betterFors`, improved for-comprehensions are now just how `for` works. Two changes matter. First, aliases may appear before any generator:

```scala
for
  base = config.retryBase          // alias first — was a syntax error
  attempt <- 1 to maxRetries
  delay = base * math.pow(2, attempt)
yield schedule(delay)
```

Second, the desugaring got smarter: a trailing alias or a trivial `yield x` no longer emits a pointless extra `.map`. That's not just aesthetics — for-heavy code in effect systems (every ZIO/cats-effect program, essentially) drops one allocation and one closure per comprehension that used to end in an identity map. You get the win by recompiling; nothing to enable.

## runtimeChecked replaces ": @unchecked" (SIP-57)

The old way to tell the compiler "I know this match isn't exhaustive, trust me" was the type ascription `: @unchecked` — easy to misplace, weird to teach. 3.8 stabilizes an extension method that says the same thing at the expression level:

```scala
val port = sys.env.get("PORT").runtimeChecked match
  case Some(p) => p.toInt          // no non-exhaustive warning;

// destructuring that used to need (xs: @unchecked)
val head :: tail = readList().runtimeChecked
```

Semantics are unchanged — failure is still a `MatchError` at runtime — but the intent is explicit and greppable: every `runtimeChecked` in a codebase is a deliberate, auditable assertion that runtime checking was chosen over static proof.

## The Scala 2 optimizer, ported (3.8.3+)

The biggest late addition to the line: 3.8.3 ports the Scala 2 JVM backend optimizer to Scala 3. The `-opt` flag enables bytecode optimizations, and `-opt-inline` scopes the inliner with patterns — `**` for everything on the classpath, `a.**` for a package subtree, `<sources>` for just your code, `!` prefixes for exclusions:

```scala
// build.sbt — inline within your own packages only
scalacOptions ++= Seq("-opt", "-opt-inline:myapp.**", "-Wopt")
```

`-Wopt` surfaces inliner warnings (e.g., a method marked `@inline` that couldn't be inlined), and `-Yopt-log-inline`/`-Yopt-trace` show decisions when you're tuning. The Scala 2 rules carry over: never inline across the boundary of libraries you don't recompile, because inlined bytecode bakes in their internals. For collections-heavy inner loops this optimizer historically bought double-digit percentages on Scala 2; having it back closes a real gap for hot-path code that the JIT alone doesn't recover. Since 3.8.4 you can append `:help` to *any* compiler flag — `scalac "-opt-inline:help"` — which is the fastest way to check pattern syntax.

## Preview and experimental: the pipeline

Under `-preview` (stable-adjacent, on track for default): **SIP-71 into conversions**, which let a library author mark parameter types as `into[T]` so callers get implicit conversions without importing `implicitConversions`; and **SIP-75 relaxed lambda syntax**, allowing single-line lambdas after a colon — `xs.map: v => v + 1`. Behind `experimental`: SIP-70 flexible varargs (`sum(0, a*, b*, 5)`), match sub-cases, and — new in 3.8.3 — **safe mode** (`import language.experimental.safe`), a language subset that rejects unchecked casts and escape hatches, aimed at code you let AI agents write. The layering is the story: preview features are one release from default, experimental ones may still change.

One structural change underneath it all: the standard library is now compiled with Scala 3 itself rather than inherited as Scala 2 bytecode. That's mostly invisible day to day, but it unblocks library evolution — the stdlib can now gain capture-checking annotations and explicit-nulls metadata, both of which 3.8 started adding for experimental modes — and it means TASTy for the whole platform, not just your code.

## Migrating

Three things bite in practice. JDK 17 is the floor — CI images running 11 stop compiling. The REPL is now a separate `scala3-repl` artifact, so tooling that embedded it needs a new dependency. And standard-library context bounds now desugar to `given` rather than `implicit`, so rare explicit calls change shape: `Array.empty(using reflect.ClassTag.Int)`. Minimum build-tool versions: sbt 1.11.5 (1.12+ recommended), Mill 1.0.5, Scala CLI 1.9.0. Go straight to 3.8.4 — it also hardened TASTy parsing against maliciously crafted files after a security audit, which matters if your tooling reads TASTy from untrusted artifacts.

**Try next:** take one hot, collections-heavy module, recompile on 3.8.4 with `-opt -opt-inline:<sources> -Wopt`, and benchmark before/after with JMH — then read the `-Yopt-log-inline` output to see what the inliner actually did.
