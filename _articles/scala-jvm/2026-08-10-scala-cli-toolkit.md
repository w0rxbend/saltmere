---
title: "Scala CLI and the Toolkit: One File to a Packaged Program"
date: 2026-08-10
track: scala-jvm
summary: "Since Scala 3.5 the `scala` command is Scala CLI. This article traces the path from a single `.scala` file carrying `//> using` directives to a tested, packaged program built on the Scala Toolkit, without a separate build tool."
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

**Gist.** Running a small Scala program historically required an external build definition — an SBT project directory and a `build.sbt` — before a single line executed. As of **Scala 3.5.0, released 22 August 2024**, the `scala` command distributed by Homebrew, SDKMAN! and Coursier *is* Scala CLI, which reads its build configuration from **using directives** embedded in the source file itself (SIP-46). The cost is that configuration is now distributed across the sources it configures rather than centralised, and the escape hatches for larger builds — multi-module layouts, custom tasks — remain outside this model.

## Using directives: configuration inside the compilation unit

A using directive is a machine-readable comment beginning with `//>`. Scala CLI parses these before compilation and derives the build from them.

```scala
//> using scala 3.5.0

@main def hello(): Unit =
  println("Saltmere says hi from Scala 3")
```

```bash
scala run hello.scala
```

`//> using scala 3.5.0` **pins the language version**; `//> using scala 3` selects the latest 3.x instead. The compiler for the requested version is downloaded on first use and cached, so subsequent runs reuse the cached compiler rather than downloading it again. The load-bearing property is that **the file describes its own environment**: transferred to another machine, `scala run hello.scala` resolves the same compiler and the same dependencies.

Directives are merged across the whole compilation, not scoped per file. When a directory contains several sources, a directive stated in any one of them applies to the set. This is what makes growing past a single file a non-event: adding `.scala` files to the directory and pointing commands at the directory is the entire migration.

```bash
scala run .
scala test .
```

## Dependencies as coordinates

`//> using dep` takes a Maven coordinate. The **double colon** in `org::artifact:version` instructs Scala CLI to append the Scala binary-version suffix to the artifact name, which is the same convention SBT's `%%` operator encodes.

```scala
//> using scala 3.5.0
//> using dep com.lihaoyi::os-lib:0.11.3

@main def countLines(): Unit =
  val here = os.list(os.pwd).filter(os.isFile)
  for f <- here do
    val n = os.read.lines(f).size
    println(s"${f.last}: $n lines")
```

`scala run` resolves the coordinate, compiles and executes in one invocation. No `libraryDependencies` entry and no build file participate.

## The Scala Toolkit

The **Scala Toolkit** is a curated bundle whose members are maintained to be mutually compatible, pulled in by a single directive:

```scala
//> using toolkit latest
```

A version may be pinned instead, for example `//> using toolkit 0.2.0`. The bundle comprises four libraries:

- **OS-Lib** — files and processes (`os.read`, `os.write`, `os.list`, subprocess invocation)
- **uPickle / uJSON** — JSON reading and writing
- **sttp** — an HTTP client
- **MUnit** — a testing framework

Selecting the bundle replaces four coordinate lookups and the compatibility question that accompanies them with one directive.

## A worked example: fetch, parse, persist

The following single file issues an HTTP GET, decodes the JSON response into case classes, and writes a tab-separated summary to disk.

```scala
//> using scala 3.5.0
//> using toolkit latest

import sttp.client4.quick.*
import upickle.default.*

case class Repo(name: String, stargazers_count: Int) derives ReadWriter

@main def report(): Unit =
  val response = quickRequest
    .get(uri"https://api.github.com/users/scala/repos?per_page=5")
    .send()

  val repos: Seq[Repo] = read[Seq[Repo]](response.body)

  val lines = repos.sortBy(-_.stargazers_count).map(r => s"${r.name}\t${r.stargazers_count}")
  val out = os.pwd / "stars.tsv"
  os.write.over(out, lines.mkString("\n"))

  println(s"Wrote ${repos.size} rows to $out")
```

Three mechanisms carry the example. First, `sttp.client4.quick.*` supplies a **synchronous backend implicitly**, so `quickRequest.get(uri"…").send()` returns a `Response[String]` exposing `.code` and `.body` with no backend construction. Second, `derives ReadWriter` **generates the uPickle codec at compile time** from the case class shape, so no reflective codec construction or hand-written reader participates at run time. Third, `os.pwd / "stars.tsv"` builds a typed `os.Path` through the `/` operator, so paths are values rather than concatenated strings.

## Tests without additional configuration

MUnit arrives with the Toolkit. Scala CLI classifies any file whose name ends in **`.test.scala`** into the test scope; no directive or directory convention is required.

```scala
//> using scala 3.5.0
//> using toolkit latest

class ReportTests extends munit.FunSuite:
  test("sorting is descending by stars"):
    val repos = Seq(Repo("a", 3), Repo("b", 10), Repo("c", 5))
    val sorted = repos.sortBy(-_.stargazers_count).map(_.name)
    assertEquals(sorted, Seq("b", "c", "a"))
```

```bash
scala test .
```

The `.` argument includes every source file in the current directory, so `report.scala` and `report.test.scala` compile as one unit and the test observes the production definitions directly.

## Packaging

Packaging commands sit behind Scala CLI's **`--power` namespace**, which gates the advanced command set. The flag may be passed per invocation or enabled once:

```bash
scala config power true
```

Four output shapes are available:

```bash
# Default: a lightweight launcher JAR
scala package report.scala -o report

# Assembly JAR — dependencies and application classes in one runnable archive
scala package report.scala -o report.jar --assembly

# GraalVM native image — a standalone native executable
scala package report.scala -o report --native-image

# Scala Native binary — no JVM involved
scala package --native report.scala -o report
```

The assembly JAR executes on any host with a JVM via `java -jar report.jar`, at the cost of JVM startup on every invocation. The `--native-image` and `--native` outputs are self-contained executables whose startup does not include JVM initialisation, which matters for commands invoked frequently and briefly. Scala CLI downloads the GraalVM or Scala Native toolchain on demand, so neither is a prerequisite of the host.

## Scope of the model

SBT and Mill remain the tools for large multi-module systems; using directives express a flat compilation, not a module graph. What the directive model removes is the fixed cost between an idea and running, tested, typed code, and it does so within the **official `scala` command** rather than an adjacent tool.

## Pitfalls

- **`//> using toolkit latest` is not reproducible.** Two builds separated in time can resolve different Toolkit versions and therefore different transitive dependencies; pin an explicit version (`//> using toolkit 0.2.0`) where reproducibility is required.
- **A directive placed in one file silently governs the others.** Because directives merge across the compilation, deleting the file that happened to carry `//> using scala 3.5.0` changes the language version for the whole directory, with the failure surfacing as unrelated compile errors elsewhere.
- **The `.test.scala` suffix is the only signal for test scope.** A test file named `ReportTest.scala` compiles into the main scope, where its MUnit dependency and its `munit.FunSuite` superclass are reported as missing or, worse, are packaged into the shipped artifact.
- **`::` versus `:` in a coordinate resolves different artifacts.** `com.lihaoyi:os-lib:0.11.3` with a single colon asks for an artifact literally named `os-lib`, which for a Scala library does not exist and fails at resolution rather than at compile time.
- **`os.write.over` truncates an existing file.** It is the overwrite variant; `os.write` on an existing path raises instead, so choosing the wrong one either destroys prior content or aborts a run that was expected to be idempotent.
- **Packaging commands are refused until `--power` is passed or enabled.** The gate is a per-invocation flag or a persisted configuration value, so a command that works on one machine fails on another whose configuration differs.
- **Compile-time codec derivation shifts failures, it does not remove them.** `derives ReadWriter` validates the case class against the declared type, not against the server's response; a JSON payload missing `stargazers_count` fails at `read` during execution.
