---
title: "sbt 2.0: What Changed, and How to Evaluate It Safely"
date: 2026-08-11
track: scala-jvm
summary: "sbt 2.0 shipped GA on 29 June 2026 — build definitions compile with Scala 3, tasks are cached by default, and the remote cache speaks a Bazel-compatible gRPC protocol. This covers the source-incompatible changes, the migration friction, and how to test a 2.x build without disturbing a working 1.x one."
reading_time: 6
tags:
  - scala
  - sbt
  - build-tools
  - scala-3
  - tooling
sources:
  - title: "sbt 2 is now available! (Scala Programming Language blog)"
    url: "https://www.scala-lang.org/blog/2026/06/29/sbt2.html"
  - title: "Migrating from sbt 1.x — The Book of sbt"
    url: "https://www.scala-sbt.org/2.x/docs/en/changes/migrating-from-sbt-1.x.html"
  - title: "sbt 2.0 change summary — The Book of sbt"
    url: "https://www.scala-sbt.org/2.x/docs/en/changes/sbt-2.0-change-summary.html"
  - title: "Remote cache setup — The Book of sbt"
    url: "https://www.scala-sbt.org/2.x/docs/en/reference/remote-cache-setup.html"
  - title: "sbt 2.x remote cache with Bazel compatibility (eed3si9n)"
    url: "https://eed3si9n.com/sbt-remote-cache-with-bazel-compat/"
---

**Gist.** A build definition in sbt is itself a compiled Scala program, so changing the language it compiles against is a source-incompatible change to every build file and every plugin in the ecosystem. **sbt 2.0, announced as generally available (GA) on 29 June 2026 with the 2.0.1 release**, moves that compilation to Scala 3, makes task-result caching the default rather than an opt-in, and adds a remote cache that speaks a Bazel-compatible protocol. The cost is paid in the plugin ecosystem: plugins are compiled artifacts, so each one must be republished for the new line before a build that depends on it can move.

The 2.x line has released frequently — **2.0.6 landed on 7 August 2026** — and **the 1.x line continues in parallel** (1.12.15 shipped the same day as 2.0.6). Migration is therefore per-project and reversible rather than forced.

## Build definitions compile with Scala 3

In sbt 2, **`build.sbt`, the files under `project/`, and all plugins are compiled by the Scala 3 compiler** (the GA announcement names Scala 3.8.4 for build definitions and plugins), and **the minimum runtime is Java Development Kit (JDK) 17**. A build with no custom task logic may compile unchanged; anything that defines its own tasks or settings must be Scala 3-clean.

Two source incompatibilities surface first because they appear in ordinary dependency declarations:

- **Postfix method call syntax is removed.** `... withSources() withJavadoc()` must be written `(...).withSources().withJavadoc()`.
- **Typeclass instance imports changed shape.** Scala 3 does not import `given` instances under a plain wildcard, so codec imports become `import sbt.librarymanagement.LibraryManagementCodec.{ given, * }`.

Two scoping changes alter the meaning of existing files rather than rejecting them, which makes them the more dangerous class of change:

- **Bare settings in `build.sbt` are injected into every subproject** rather than applying to the root project. A setting intended for the root alone must name it: `LocalRootProject / name := "root"`.
- **Slash syntax is mandatory.** The older axis notation `test:compile` is removed entirely; only `Test / compile` is accepted. Builds already converted on 1.x — slash syntax has been available since sbt 1.1 — need no work here.

## Caching is the default execution model

sbt 2 treats caching as a property of task execution rather than a feature layered over it.

- **Task results are cached by default.** A task's result is serialized and reused when its inputs are unchanged. The invariant this requires is that **a cached task's result type must have a `JsonFormat` given instance**; a task whose result cannot be encoded that way has to be wrapped in `Def.uncached { ... }`.
- **Unchanged tests are skipped by default,** the behaviour sbt 1.x offered only through the separate `testQuick` command.
- **A remote cache** extends reuse across machines and continuous-integration (CI) agents. **The backend protocol is gRPC and is compatible with Bazel remote-cache servers** — bazel-remote, BuildBuddy, EngFlow and NativeLink are the servers named in the design write-up — so `compile` and `test` outputs can be fetched instead of recomputed.

Enabling the remote cache is two edits. First, the plugin in `project/plugins.sbt`:

```scala
addRemoteCachePlugin
```

Then a backend in `build.sbt`:

```scala
// Unauthenticated local gRPC cache
Global / remoteCache := Some(uri("grpc://localhost:2024"))

// Or a hosted cache over mutual TLS
Global / remoteCache := Some(uri("grpcs://localhost:2024"))
Global / remoteCacheTlsCertificate := Some(file("/tmp/sslcert/ca.crt"))
Global / remoteCacheTlsClientCertificate := Some(file("/tmp/sslcert/client.crt"))
Global / remoteCacheTlsClientKey := Some(file("/tmp/sslcert/client.pem"))
```

The `grpc` scheme is unencrypted; `grpcs` is the TLS scheme, and the documented configuration for it sets all three certificate settings above. Caches that authenticate with an API key take it through `remoteCacheHeaders` instead.

**sbtn**, a native client, talks to a resident sbt server, so Java virtual machine (JVM) start-up is not paid on every invocation.

## Cross-building and dependency resolution

Two capabilities previously supplied by separate components are now part of sbt itself:

- **Project matrix is built in.** Cross-building a project across the JVM, Scala.js and Scala Native no longer requires the `sbt-projectmatrix` plugin. A consequence for dependency declarations: **`%%` now encodes both the Scala version and the platform suffix, so the `%%%` operator that Scala.js and Scala Native plugins added to work around its absence is no longer needed.**
- **Coursier is the standard resolver.** Coursier has been the default since sbt 1.3 and is the library-management path in 2.x, so resolution is parallel and locally cached by default.

A minimal build definition valid under sbt 2:

```scala
ThisBuild / scalaVersion := "3.8.4"
ThisBuild / organization := "com.saltmere"

lazy val root = (project in file("."))
  .settings(
    name := "hello-sbt2",
    libraryDependencies += "org.scalameta" %% "munit" % "1.3.5" % Test
  )
```

## Version pinning makes evaluation reversible

The sbt version is declared per repository in `project/build.properties`. The launcher reads that file and downloads the matching sbt distribution, so **the version in use is a property of the checkout, not of the machine's global installation**:

```properties
sbt.version=2.0.6
```

This is what makes an evaluation cheap. Bumping `build.properties` to `2.0.6` on a branch and running `sbt compile` exercises the migration; because the 1.x and 2.x lines coexist and the version is tracked in the repository, `git switch` back to a branch pinned at, for example, `1.12.15` restores the previous toolchain without any uninstall step. Copying the project into a scratch directory achieves the same isolation.

## Plugin availability is the binding constraint

The dominant migration cost is **plugins, because a plugin is compiled code and must be republished against sbt 2 and Scala 3 before a 2.x build can load it**. Several widely used plugins support sbt 2 — **Scala.js 1.22.0, Scala Native 0.5.11, and sbt-assembly 2.3.1** — but many community plugins have not been cross-published. The Scala Center's **`sbt2-compat`** plugin exists to let plugin authors cross-build a single source tree for both lines. The practical consequence is that `plugins.sbt` should be audited before a migration is committed to: a build whose critical plugin has no sbt 2 release cannot migrate, regardless of how clean its own sources are.

For single-file scripts and small tools, this machinery is not warranted; Scala CLI covers that case (see the *Scala CLI + the Toolkit* article). sbt 2 targets multi-module builds where caching and cross-building are load-bearing.

## Pitfalls

- **A setting written bare at the top of `build.sbt` now applies to every subproject.** A build that previously set `name` once at the root silently renames all subprojects; the fix is `LocalRootProject / name`.
- **A task whose result type has no `JsonFormat` instance fails once caching is on.** The default caching path encodes results as JSON, so such a task must be wrapped in `Def.uncached { ... }`.
- **`%%%` is no longer the cross-platform operator.** With project matrix built in and `%%` carrying the platform suffix, cross-platform dependencies are declared with `%%`; whether a leftover `%%%` still compiles depends on the platform plugin version in use.
- **`test:compile`-style axis notation is rejected outright.** Only slash syntax (`Test / compile`) is parsed in 2.x, so scripts and CI invocations carrying the old form break before any compilation starts.
- **A wildcard import of a codec object no longer brings its instances into scope.** Scala 3 requires `{ given, * }`, and the symptom is a missing-implicit error rather than an unresolved-name error.
- **Running on a JDK older than 17 fails at launch.** The 2.x minimum runtime is JDK 17, so a CI image pinned to an earlier JDK fails before the build definition is read.
- **A plugin without an sbt 2 release blocks the whole migration.** The failure occurs when `project/` is compiled, so it appears before any project source is touched.
