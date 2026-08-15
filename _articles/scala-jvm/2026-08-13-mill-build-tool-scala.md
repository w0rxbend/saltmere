---
title: "Mill: builds as a typed object graph, not a task DAG to configure"
date: 2026-08-13
track: scala-jvm
summary: "Mill models a build as Scala objects whose methods are cached tasks. Mill 1.0 added a native launcher, a YAML build header, and an install that needs no host JVM. This article covers the object model, the caching mechanism, and the contrast with sbt."
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

**Gist.** A build tool must know which work to redo after a change, which requires a dependency graph that is both explicit and trustworthy. Mill obtains that graph from the language itself: a build is a Scala object, each build target is a method on it, and an edge exists exactly where one method calls another, with every target's result cached on disk under `out/` and keyed on the hashes of its inputs. The cost is that the graph is only as accurate as the method calls — work performed outside the recorded inputs, such as reading an environment variable or a file that no target declares, is invisible to invalidation and produces stale results.

**Mill 1.0.0 shipped in 2025**; the line described here is **1.1.8**. Mill 1.0 made the tool self-contained: a native launcher that avoids Java Virtual Machine (JVM) startup, a YAML build header, and an installer that downloads its own JVM, so no JVM is required on the host. The release notes describe the launcher as reducing startup latency; no published measurement is quoted here.

## The object model

A build lives in `build.mill` at the repository root. Modules are `object`s extending a trait such as `ScalaModule`; settings are `def`s that override members of that trait. The `//|` header block, written in YAML, configures Mill itself rather than the project.

```scala
//| mill-version: 1.1.8

package build
import mill.*, scalalib.*

object app extends ScalaModule:
  def scalaVersion = "3.7.0"
  def mvnDeps = Seq(
    mvn"org.typelevel::cats-effect:3.5.4"
  )

  object test extends ScalaTests:
    def mvnDeps = Seq(mvn"org.scalameta::munit:1.0.0")
    def testFramework = "munit.Framework"
```

That is a complete two-module build: a main module and its test submodule, expressed as a real nested `object`. **Module nesting is lexical nesting**, so the task path on the command line follows the object path in the source. Multi-module projects add more objects, and one module depends on another through `def moduleDeps = Seq(app)`. There is no separate `project/` build-of-the-build directory; the `//|` header carries the bootstrap information that sbt keeps in `project/build.properties`.

## Targets and tasks

Every no-argument override is a **target** (`Task`): a cached node in the graph. `def sources`, `def compile` and `def mvnDeps` are all targets. Mill invokes them by path from the command line:

```bash
./mill app.compile          # compile the app module
./mill app.test             # compile dependencies as needed, then run tests
./mill app.run --arg foo    # run the main class
./mill show app.assembly    # build a fat jar, print its path as JSON
./mill resolve app._        # list every task under app
```

`resolve` and `show` are the discovery commands. `resolve _` prints the task tree without running anything; `show` evaluates a target and prints its return value as JavaScript Object Notation (JSON), which makes the value a target produces inspectable from a shell script rather than only from inside the build.

## Caching is structural

Each target's result is written to disk under `out/`, keyed on the hashes of its inputs and on the results of the targets it depends on. Re-running a target performs no work unless one of those hashes changed, and because the graph is explicit, Mill can determine which downstream targets the change reaches. Incrementality is not a per-target opt-in; it follows from the model. The Mill documentation claims faster builds than the alternatives it compares against; the comparison is the project's own, not an independent benchmark.

- A target's `out/<name>.json` holds its cached value together with the input hashes it was computed from.
- Changing a source file re-runs `compile` and the targets reachable from it, and nothing else.
- Changing nothing makes an invocation a no-op that returns without recomputation.
- `./mill clean app.compile` discards the cache entry for one target rather than the whole `out/` tree.

The invariant the cache depends on is that **a target's output is a pure function of the values it reads through task calls**. Custom tasks declare dependencies by *calling* other tasks, and the call is what records the edge:

```scala
def lineCount = Task {
  sources().flatMap(pr => os.walk(pr.path))
    .filter(_.ext == "scala")
    .map(os.read.lines(_).size).sum
}
```

`sources()` is an applied call, so Mill records the edge and re-runs `lineCount` only when `sources` changes. A value captured from outside the block — read from the ambient environment rather than from another task — is not part of the key, and the cached result survives changes to it.

### Implementation sketch (Scala)

The load-bearing idea is small enough to state directly: a node whose cached value is invalid whenever the hash of its own inputs, combined with the keys of its dependencies, differs from the key stored beside the value.

```scala
final case class Key(value: Int)

trait Node[A]:
  def name: String
  def deps: Seq[Node[?]]
  def inputHash: Int              // hashes of files, versions, literals
  def compute(): A

final class Cache:
  private val stored = collection.mutable.Map.empty[String, (Key, Any)]

  /** Key of a node folds its own input hash with the keys of its dependencies,
    * so a change anywhere upstream changes the key here. */
  def keyOf(n: Node[?]): Key =
    Key(n.deps.map(keyOf(_).value).foldLeft(n.inputHash)(_ * 31 + _))

  def eval[A](n: Node[A]): A =
    val k = keyOf(n)
    stored.get(n.name) match
      case Some((`k`, v)) => v.asInstanceOf[A]      // hit: upstream unchanged
      case _ =>
        n.deps.foreach(eval(_))                      // evaluate upstream first
        val v = n.compute()
        stored.update(n.name, (k, v))
        v
```

Two properties of this sketch mirror Mill's behaviour. First, invalidation is transitive without a separate propagation pass, because a dependency's key is folded into the dependent's key. Second, a value read inside `compute()` but absent from `inputHash` never affects the key, which is precisely the class of bug that undeclared inputs produce.

## Mill and sbt compared

| Concern         | sbt                             | Mill                              |
|-----------------|---------------------------------|-----------------------------------|
| Build is        | settings/tasks in a DSL         | Scala objects and their methods   |
| Dependencies    | `.dependsOn`, macro-expanded    | plain method calls in `Task {}`   |
| Incrementality  | task caching added in sbt 2     | structural, keyed on input hashes |
| Startup         | JVM warmup / server             | native launcher, no JVM warmup    |
| Bootstrap file  | `project/build.properties`      | `//\|` header in `build.mill`     |

sbt 2 narrowed the gap by adding task caching, but the models remain distinct: an sbt build is described in a settings vocabulary layered over Scala, while a Mill build is ordinary Scala definitions whose call structure is the graph. Metals supports Mill as a build tool, so editor integration does not depend on exporting to another format.

Installation does not touch installed JVMs:

```bash
curl -L https://github.com/com-lihaoyi/mill/releases/download/1.1.8/mill \
  -o mill && chmod +x mill
./mill --version
```

## Pitfalls

- **A task that reads an environment variable or an undeclared file inside `Task {}` is cached against inputs that omit it.** The value changes, the key does not, and the stale result is returned until an unrelated input forces recomputation.
- **A dependency expressed as a reference rather than a call records no edge.** Writing `sources` instead of `sources()` yields the task object, not its value, so Mill has nothing to invalidate on.
- **`./mill clean` without a task path discards the entire `out/` tree.** The next invocation is a full rebuild; `./mill clean app.compile` removes one entry.
- **The `//|` header pins `mill-version`, so the launcher in the repository and the version the build runs under are separate facts.** A build that works locally can resolve a different Mill version elsewhere if the header and the checked-in launcher disagree.
- **Module paths on the command line follow object nesting, not directory layout.** A submodule declared inside `object app` is addressed as `app.test` regardless of where its sources sit on disk.
