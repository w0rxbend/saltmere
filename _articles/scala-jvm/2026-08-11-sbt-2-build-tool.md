---
title: "sbt 2.0 Is Here: What Actually Changed, and How to Try It Safely"
date: 2026-08-11
track: scala-jvm
summary: "sbt 2.0 shipped GA on 29 June 2026 — build definitions now compile with Scala 3, tasks are cached by default, and there's a Bazel-compatible remote cache. Here's what's new, the migration friction, and how to test it without breaking your 1.x build."
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

After a long milestone-and-RC road, **sbt 2.0.0 shipped as a stable GA release on 29 June 2026**. This is not a marketing "2.0" — it is a genuine major version with source-incompatible changes. As of this writing the line has already moved fast: **2.0.6 landed on 7 August 2026**, roughly every two weeks, and the **sbt 1.x line continues in parallel** (1.12.x is still maintained). So there is no forced march. You can adopt sbt 2 deliberately, project by project.

This article covers what changed, where the friction is, and how to kick the tires without wrecking a working 1.x build.

## Build definitions now compile with Scala 3

The headline change: **your `build.sbt`, `project/*.scala`, and plugins are now compiled with Scala 3** (sbt 2.0.x uses the Scala 3.8.x compiler), and the minimum runtime is **JDK 17**. For simple builds you will barely notice, but any custom task logic or plugin code has to be Scala 3-clean.

Two syntax changes bite immediately:

- **Postfix method calls are gone.** `... withSources() withJavadoc()` must become `(...).withSources().withJavadoc()`.
- **Typeclass imports changed.** Where you imported codecs with a wildcard, you now write `import sbt.librarymanagement.LibraryManagementCodec.{ given, * }` to bring `given` instances into scope.

Also worth knowing: **bare settings in `build.sbt` are now injected into every subproject**, not the root. If you really mean "root only," scope it: `LocalRootProject / name := "root"`. And **slash syntax is now mandatory** — the old `test:compile` axis notation is fully removed in favor of `Test / compile`. If you already migrated to slash syntax on 1.x (available since 1.1), you're ahead.

## Caching is the real story

sbt 2 makes caching a first-class, default behavior rather than a bolt-on.

- **All tasks are cached by default.** Task results are serialized and reused when inputs are unchanged. If a task returns something non-serializable, wrap it: `Def.uncached(...)`.
- **`test` runs incrementally by default** — only affected tests re-run.
- **A remote build cache** shares those results across machines and CI. The backend speaks **gRPC and is compatible with Bazel remote-cache servers** (BuildBuddy, bazel-remote, etc.), so `compile` and `test` outputs can be pulled instead of recomputed.

Wiring up a remote cache is two steps. Add the plugin in `project/plugins.sbt`:

```scala
addRemoteCachePlugin
```

Then point it at a backend in `build.sbt`:

```scala
// Unauthenticated local gRPC cache
Global / remoteCache := Some(uri("grpc://localhost:2024"))

// Or a hosted cache over mTLS
Global / remoteCache := Some(uri("grpcs://cache.example.io"))
Global / remoteCacheTlsCertificate := Some(file("/tmp/ssl/ca.crt"))
Global / remoteCacheTlsClientCertificate := Some(file("/tmp/ssl/client.crt"))
Global / remoteCacheTlsClientKey := Some(file("/tmp/ssl/client.pem"))
```

There's also **sbtn**, the native-image client that keeps a warm server process around so start-up feels near-instant. The `sbt` runner detects the build version and launches sbtn automatically for 2.x builds.

## Cross-building and dependency management

Two more consolidations:

- **Project matrix is built in.** Cross-building across JVM / Scala.js / Scala Native no longer needs `sbt-projectmatrix` as a separate plugin — the machinery is native to sbt 2. A knock-on effect: platform-cross-published libraries use the ordinary `%%` operator; the old `%%%` operator from Scala.js/Native is gone.
- **Coursier is the resolver.** Coursier (the default since 1.3) is now the standard library-management path in sbt 2, so parallel, cached artifact resolution is just how dependencies work.

Here is a minimal `build.sbt` that is valid under sbt 2:

```scala
ThisBuild / scalaVersion := "3.8.4"
ThisBuild / organization := "com.saltmere"

lazy val root = (project in file("."))
  .settings(
    name := "hello-sbt2",
    libraryDependencies += "org.scalameta" %% "munit" % "1.0.4" % Test
  )
```

## Setting the version — and trying it without breaking 1.x

sbt's version is pinned per project in `project/build.properties`. The `sbt` launcher reads that file and downloads the matching sbt, so switching versions is a one-line change that doesn't touch your global install:

```properties
sbt.version=2.0.6
```

The safe way to experiment: **do it on a branch.** Bump `build.properties` to `2.0.6`, run `sbt compile`, and see what breaks — your `main` branch (still on, say, `1.12.15`) is untouched because the version lives in the repo. Because the two lines coexist, `git switch main` restores your 1.x world instantly. You can also copy a project to a scratch directory and migrate there first.

## The honest part: plugin friction

The biggest migration cost is **plugins, because they are compiled code and must be rebuilt for sbt 2 / Scala 3**. Many core plugins are ready — Scala.js 1.22.0, Scala Native 0.5.11, and sbt-assembly 2.3.1 all support sbt 2 — but plenty of community plugins are not yet cross-published. The Scala Center's **`sbt2-compat`** plugin helps plugin authors cross-build sources for both lines, which is smoothing adoption, but you should **audit your `plugins.sbt` before committing to a migration**. If a critical plugin has no sbt 2 release, wait.

For quick scripts and one-file tools, none of this ceremony is warranted — reach for Scala CLI instead (see the *Scala CLI + the Toolkit* article). sbt 2 is for real multi-module builds where caching and cross-building earn their keep.

**Try next:** On a throwaway branch, set `sbt.version=2.0.6` in `project/build.properties`, run `sbt compile test`, and grep the output for `Def.uncached` and postfix-syntax errors to gauge your migration surface.
