---
title: "Scala CLI + the Toolkit: One File to a Real Program, No SBT"
date: 2026-08-10
track: scala-jvm
summary: "Since Scala 3.5 the `scala` command IS Scala CLI. Here's how to go from a single-file `.scala` script with `//> using` directives to a tested, packaged program using the Scala Toolkit — no build tool ceremony."
reading_time: 6
tags:
  - scala
  - scala-cli
  - scala-toolkit
  - tooling
  - scala-3
sources:
  - title: "Scala 3.5.0 released! (Scala CLI as the default runner)"
    url: "https://www.scala-lang.org/blog/2024/08/22/scala-3.5.0-released.html"
  - title: "SIP-46 — Scala CLI as default Scala command"
    url: "https://docs.scala-lang.org/sips/scala-cli.html"
  - title: "The Scala Toolkit (announcement + bundled libraries)"
    url: "https://www.scala-lang.org/blog/2023/06/20/toolkit.html"
  - title: "Scala Toolkit — Introduction (docs.scala-lang.org)"
    url: "https://docs.scala-lang.org/toolkit/introduction.html"
  - title: "Scala CLI — package command (assembly, native, native-image)"
    url: "https://scala-cli.virtuslab.org/docs/commands/package/"
---

For years the first thing you learned about Scala was also the most discouraging: before you could run ten lines of code, you needed SBT, a `build.sbt`, a `project/` folder, and a plugin or two. That tax is gone. As of **Scala 3.5.0** (released 22 August 2024), the `scala` command you get from Homebrew, SDKMAN!, or Coursier *is* Scala CLI. Installing Scala now gives you one binary that compiles, runs, tests, and even publishes to Maven Central — no separate tool, no build file required to start.

This article walks the "understand by building" path: start with a single file, add real dependencies with one-line comments, pull in the Scala Toolkit for everyday work (files, JSON, HTTP), write a test, then produce a binary. Everything below is a real, runnable file.

## Hello, single file

Create `hello.scala`:

```scala
//> using scala 3.5.0

@main def hello(): Unit =
  println("Saltmere says hi from Scala 3")
```

Run it:

```bash
scala run hello.scala
```

The first line is a **using directive** — a machine-readable comment starting with `//>`. It configures the build *inside the source file*, so there is no external config to keep in sync. `//> using scala 3.5.0` pins the language version (you can also write `//> using scala 3` for the latest 3.x). Scala CLI downloads the compiler on first use and caches it.

That is the whole trick: the file describes its own environment. Copy it to a colleague, and `scala run hello.scala` does the same thing on their machine.

## Adding a dependency in one line

Need a library? Add another directive. `//> using dep` takes a standard Maven coordinate in Scala's `org::artifact::version` form (the `::` tells Scala CLI to append the Scala version suffix):

```scala
//> using scala 3.5.0
//> using dep com.lihaoyi::os-lib:0.11.3

@main def countLines(): Unit =
  val here = os.list(os.pwd).filter(os.isFile)
  for f <- here do
    val n = os.read.lines(f).size
    println(s"${f.last}: $n lines")
```

No `build.sbt`, no `libraryDependencies +=`. `scala run` resolves the coordinate, compiles, and runs.

## The Scala Toolkit: batteries in one directive

Wiring individual coordinates is fine, but for everyday scripting there is a curated bundle: the **Scala Toolkit**. One directive pulls in a compatible set of libraries maintained to work together:

```scala
//> using toolkit latest
```

The Toolkit bundles four libraries (you can also pin a version, e.g. `//> using toolkit 0.2.0`):

- **OS-Lib** — files and processes (`os.read`, `os.write`, `os.list`, subprocess calls)
- **uPickle / uJSON** — reading and writing JSON
- **sttp** — an HTTP client
- **MUnit** — a testing framework

That covers a huge share of "I just need to get something done" tasks without hunting for coordinates.

## A real example: fetch, parse, save

Here is a single file that hits an HTTP endpoint, parses the JSON response with uPickle, and writes a summary to disk with OS-Lib. This is the kind of glue script people reach for Python for — and it is a legitimate, typed Scala program.

```scala
//> using scala 3.5.0
//> using toolkit latest

import sttp.client4.quick.*
import upickle.default.*

case class Repo(name: String, stargazers_count: Int) derives ReadWriter

@main def report(): Unit =
  // 1. HTTP GET with sttp's quick API — no backend to set up
  val response = quickRequest
    .get(uri"https://api.github.com/users/scala/repos?per_page=5")
    .send()

  // 2. Parse the JSON body into typed case classes with uPickle
  val repos: Seq[Repo] = read[Seq[Repo]](response.body)

  // 3. Build a report and write it with OS-Lib
  val lines = repos.sortBy(-_.stargazers_count).map(r => s"${r.name}\t${r.stargazers_count}")
  val out = os.pwd / "stars.tsv"
  os.write.over(out, lines.mkString("\n"))

  println(s"Wrote ${repos.size} rows to $out")
```

Run it the same way:

```bash
scala run report.scala
```

A few things worth noticing. `quickRequest.get(uri"...").send()` needs no explicit backend — the `quick` import wires a synchronous one for you, returning a `Response[String]` with `.code` and `.body`. `read[Seq[Repo]]` maps JSON straight onto case classes because `derives ReadWriter` generates the codec at compile time; a typo in a field name is a *compile error*, not a 2 a.m. production surprise. And `os.write.over` takes an `os.Path` built with the `/` operator, so paths are values, not fragile strings.

## Writing a test

The Toolkit ships MUnit, so tests need no extra setup. Put testable logic in a function and assert on it. Create `report.test.scala`:

```scala
//> using scala 3.5.0
//> using toolkit latest

class ReportTests extends munit.FunSuite:
  test("sorting is descending by stars"):
    val repos = Seq(Repo("a", 3), Repo("b", 10), Repo("c", 5))
    val sorted = repos.sortBy(-_.stargazers_count).map(_.name)
    assertEquals(sorted, Seq("b", "c", "a"))
```

Scala CLI treats files ending in `.test.scala` as the test scope automatically. Run them:

```bash
scala test .
```

Passing `.` tells it to include every source file in the current directory, so your `report.scala` and its test compile together as one small project — still with zero build files.

## Growing past one file

When one file gets crowded, just add more `.scala` files in the directory and point commands at the directory instead of a single file:

```bash
scala run .
scala test .
```

Shared directives (like `//> using toolkit latest`) can live in any file; Scala CLI merges them across the whole compilation. There is no "convert to a project" step — you were already in one.

## Packaging: from script to binary

Running from source is great for iteration, but eventually you want an artifact you can hand off. Packaging lives behind Scala CLI's `--power` mode (a namespace for advanced commands). Enable it once, or pass `--power` per call:

```bash
scala config power true
```

Then choose your output:

```bash
# Default: a lightweight launcher JAR
scala package report.scala -o report

# Fat/assembly JAR — dependencies + your code in one runnable JAR
scala package report.scala -o report.jar --assembly

# GraalVM native image — a standalone native executable, fast startup
scala package report.scala -o report --native-image

# Scala Native binary (no JVM at all)
scala package --native report.scala -o report
```

The assembly JAR runs anywhere with a JVM (`java -jar report.jar`). The `--native-image` and `--native` outputs give you a self-contained binary with near-instant startup — ideal for CLI tools you invoke often. Scala CLI downloads GraalVM or the Scala Native toolchain as needed, so you do not pre-install anything.

## Why this matters

The point is not that build tools are bad — SBT and Mill earn their keep on large, multi-module systems. The point is that the *distance from idea to running, tested, typed code* just collapsed. A using directive is a build file you can read in one glance and that travels inside the code it configures. You learn Scala by writing Scala, not by learning a build DSL first. And because it is the *official* `scala` command now, this is not a side experiment — it is the front door.

**Try next:** Rewrite one of your throwaway shell or Python glue scripts as a single `.scala` file with `//> using toolkit latest`. Read a file with `os.read`, transform it, and write the result back with `os.write.over`. Then run `scala package --native-image` and drop the resulting binary in your `PATH`.
