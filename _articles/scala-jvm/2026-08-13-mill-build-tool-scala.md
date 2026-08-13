---
title: "Mill: builds as a typed object graph, not a task DAG you configure"
date: 2026-08-13
track: scala-jvm
summary: "Mill models your build as Scala objects whose methods are cached tasks. Mill 1.0 landed a native launcher, a YAML build header, and JVM-free install. Here's the object model, how caching works, and how it contrasts with sbt."
reading_time: 6
tags: [scala, mill, build-tools, caching, tooling, scala-jvm]
sources:
  - title: "Mill: A Better Build Tool for Java, Scala, & Kotlin (docs home)"
    url: "https://mill-build.org/mill/index.html"
  - title: "Mill Build Tool v1.0.0 Release Highlights"
    url: "https://mill-build.org/blog/13-mill-build-tool-v1-0-0.html"
  - title: "Introduction to Mill for Scala"
    url: "https://mill-build.org/mill/scalalib/intro.html"
  - title: "Releases · com-lihaoyi/mill (GitHub)"
    url: "https://github.com/com-lihaoyi/mill/releases"
  - title: "Mill · Metals (build tool support)"
    url: "https://scalameta.org/metals/docs/build-tools/mill.html"
---

sbt's mental model is a giant map of settings and tasks that a DSL mutates. Mill's is the opposite: your build *is* a Scala object, your build targets *are* its methods, and the dependency graph is just which method calls which. If you can read Scala, you can read a Mill build — there's no separate settings algebra to learn. **Mill 1.0.0 shipped 10 July 2025**; the current line is **1.1.8**, and 1.0 was the release that made the tool self-contained: a Graal native launcher with ~100 ms startup, a YAML build header, and an installer that downloads its own JVM so nothing is required on the host.

## The object model

A build lives in `build.mill` at the repo root. Modules are `object`s that extend a trait like `ScalaModule`; the settings are `def`s you override. The `//|` header block configures Mill itself.

```scala
//| mill-version: 1.1.8

package build
import mill.*, scalalib.*

object app extends ScalaModule:
  def scalaVersion = "3.7.0"
  def mvnDeps = Seq(
    mvn"org.typelevel::cats-effect:3.7.0"
  )

  object test extends ScalaTests:
    def mvnDeps = Seq(mvn"org.scalameta::munit:1.0.0")
    def testFramework = "munit.Framework"
```

That's a complete two-module build: a main module and its test submodule, nested as a real inner `object`. Multi-module projects are just more objects; one depends on another with `def moduleDeps = Seq(app)`. There is no `lazy val` soup and no separate `project/` build-of-the-build — the header handles bootstrap.

## Targets and tasks

Every no-argument override is a **target** (`Task`): a cached node in the graph. `def sources`, `def compile`, `def mvnDeps` are all targets. Mill invokes them by path from the CLI:

```bash
./mill app.compile          # compile just the app module
./mill app.test             # compile deps as needed, then run tests
./mill app.run --arg foo    # run the main class
./mill show app.assembly    # build a fat jar, print its path as JSON
./mill resolve app._        # list every task under app
```

`resolve` and `show` are the discovery tools: `resolve _` prints the whole task tree, `show` runs a target and prints its return value as JSON — handy for scripting and for seeing exactly what a task produces.

## Caching is structural, not opt-in

This is where Mill earns its "3–7x faster" claim. Each target's result is cached to disk in `out/`, keyed on the hashes of its inputs and its dependency results. Re-running a target does nothing unless an input changed — and because the graph is explicit, Mill knows precisely which downstream targets to invalidate. You don't configure incrementality; it's how the model works.

- A target's `out/<name>.json` holds its cached value and input hashes.
- Change a source file → only `compile` and its dependents re-run.
- Change nothing → the whole build is a no-op that returns instantly.
- `./mill clean app.compile` drops one target's cache surgically.

Custom tasks compose the same way — declare dependencies by *calling* other tasks:

```scala
def lineCount = Task {
  sources().flatMap(pr => os.walk(pr.path))
    .filter(_.ext == "scala")
    .map(os.read.lines(_).size).sum
}
```

`sources()` is a call, so Mill records the edge and re-runs `lineCount` only when sources change.

## Mill vs sbt at a glance

| Concern         | sbt                             | Mill                              |
|-----------------|---------------------------------|-----------------------------------|
| Build is        | settings/tasks in a DSL         | Scala objects and their methods   |
| Dependencies    | `.dependsOn`, macro-expanded    | plain method calls in `Task {}`   |
| Incrementality  | task caching (default in sbt 2) | structural, keyed on input hashes |
| Startup         | JVM warmup / server             | native launcher, ~100 ms          |
| Bootstrap file  | `project/build.properties`      | `//|` header in `build.mill`      |
| Learning curve  | a second language               | the Scala you already know        |

sbt 2 closed part of the gap by caching tasks by default, but the models still differ: sbt asks you to describe a build in its vocabulary; Mill asks you to write objects and hits the cache for free. For a new Scala or Java repo — especially a monorepo where fast, predictable incremental builds matter — Mill is the lower-ceremony choice.

Install without touching your JVMs:

```bash
curl -L https://github.com/com-lihaoyi/mill/releases/download/1.1.8/mill \
  -o mill && chmod +x mill
./mill --version
```

**Try next:** drop the `build.mill` above into an empty repo, run `./mill app.compile` twice, and watch the second run finish in milliseconds — then `./mill show app.assembly` to see the cached jar path.
