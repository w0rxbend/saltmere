---
title: "Apache Pekko: The Akka Fork, and Porting an Actor System Off the BSL"
date: 2026-08-15
track: scala-jvm
summary: "In September 2022 Lightbend relicensed Akka under the Business Source License, and the community forked the last Apache-2.0 Akka 2.6.x into Apache Pekko. This article covers the fork's provenance, the modules Pekko ships (actors, streams, HTTP, cluster), and the mechanical migration — dependency, import, config and wire-identifier renames — with a Scala 3 typed actor and current versions as of August 2026 (Pekko core 1.6.0)."
reading_time: 6
tags: [pekko, akka, actors, scala-3, migration, apache]
sources:
  - title: "Apache Pekko™ — project home"
    url: "https://pekko.apache.org/"
  - title: "Why we are changing the license for Akka — akka.io"
    url: "https://akka.io/blog/why-we-are-changing-the-license-for-akka"
  - title: "Migration from Akka to Apache Pekko — Pekko documentation"
    url: "https://pekko.apache.org/docs/pekko/1.3/migration/migration-guide-akka-1.0.x.html"
  - title: "Download — Apache Pekko (current module versions)"
    url: "https://pekko.apache.org/download.html"
  - title: "Akka no longer Open Source — InfoQ"
    url: "https://www.infoq.com/news/2022/09/akka-no-longer-open-source/"
---

**Gist.** From Akka 2.7 onward the actor toolkit ships under the Business Source License (BSL) 1.1, which is source-available rather than open source: production use above a revenue threshold requires a commercial license, and each release converts to Apache-2.0 only after a three-year delay. Apache Pekko resolves this for existing deployments by forking the last Apache-2.0 release, **Akka 2.6.20**, and renaming every namespace, configuration key and wire identifier under Apache Software Foundation governance. The cost of the rename is that it reaches the network layer: **an Akka node and a Pekko node cannot join the same cluster**, so a distributed migration is a cutover rather than a rolling upgrade.

## Provenance of the fork

Lightbend announced the license change on 7 September 2022. It applied going forward only; Akka 2.6.20 remained where it was, freely licensed and frozen at that point in its history. That frozen artifact is the seed of the fork. Volunteers donated it to the Apache Software Foundation, and **Apache Pekko is a hard fork of Akka 2.6.x** carrying Apache-2.0 forward, maintained independently since.

Pekko is not a reimplementation. Its behaviour is Akka 2.6.x behaviour, because it is that code with identifiers substituted. The module set carried over intact:

- **pekko-actor** and **pekko-actor-typed** — the classic and typed actor runtimes.
- **pekko-stream** — a Reactive Streams implementation with back-pressure.
- **pekko-http** — the HTTP server and client stack, formerly akka-http.
- **pekko-cluster**, **pekko-cluster-sharding**, **pekko-persistence**, **pekko-projection** — the distributed and event-sourcing components.
- **pekko-connectors** — the integration library formerly named Alpakka.

As of August 2026 the current line is **Pekko core 1.6.0**, with **pekko-http 1.4.0** versioned separately as it was under Akka. Releases cross-publish for **Scala 2.12, 2.13 and 3** (the 3.3 long-term-support line). The 1.x line is the release recommended for production; a 2.0.0 line exists only as milestones aimed at library maintainers. Because the fork began from a mature codebase rather than a green field, 1.x releases have consisted largely of dependency refreshes, Java Development Kit (JDK) compatibility work, and bug fixes.

### Implementation sketch (Scala)

A minimal typed actor under Pekko is indistinguishable from Akka Typed apart from the package prefix. State is carried in the returned `Behavior` rather than in a mutable field, the protocol is an explicit sealed trait, and the `ActorSystem` is itself a typed `ActorRef` addressing the guardian behaviour.

```scala
//> using scala 3.3.6
//> using dep org.apache.pekko::pekko-actor-typed:1.6.0

import org.apache.pekko.actor.typed.*
import org.apache.pekko.actor.typed.scaladsl.Behaviors

object Counter:
  sealed trait Command
  case object Increment                             extends Command
  final case class GetValue(replyTo: ActorRef[Int]) extends Command

  def apply(n: Int = 0): Behavior[Command] =
    Behaviors.receiveMessage:
      case Increment     => apply(n + 1)          // next behavior carries the state
      case GetValue(who) => who ! n; Behaviors.same

@main def run(): Unit =
  val system: ActorSystem[Counter.Command] =
    ActorSystem(Counter(), "counter")
  system ! Counter.Increment
  system ! Counter.Increment
```

Nothing in that listing is Pekko-specific: source written against Akka Typed compiles against Pekko once its imports are rewritten.

## The migration is a rename

Porting an Akka 2.6.x project is mechanical because the target is the same code under different names. Four substitutions cover most of a codebase.

**1. Dependencies.** The group identifier changes from `com.typesafe.akka` to `org.apache.pekko`, and the artifact prefix from `akka-` to `pekko-`. Versions are renumbered from the Akka 2.6.x series into Pekko's own 1.x series.

```scala
// before (Akka 2.6.20 — Apache-2.0, frozen)
libraryDependencies ++= Seq(
  "com.typesafe.akka" %% "akka-actor-typed" % "2.6.20",
  "com.typesafe.akka" %% "akka-stream"      % "2.6.20"
)

// after (Apache Pekko)
libraryDependencies ++= Seq(
  "org.apache.pekko" %% "pekko-actor-typed" % "1.6.0",
  "org.apache.pekko" %% "pekko-stream"      % "1.6.0"
)
```

**2. Imports.** The package tree moved wholesale from `akka.*` to `org.apache.pekko.*`.

```scala
- import akka.actor.typed.*
- import akka.stream.scaladsl.*
+ import org.apache.pekko.actor.typed.*
+ import org.apache.pekko.stream.scaladsl.*
```

**3. Configuration.** The Human-Optimized Config Object Notation (HOCON) root key changes from `akka` to `pekko`. Keys nested beneath it keep their paths.

```hocon
# application.conf
pekko {
  actor.provider = cluster
  remote.artery.canonical.hostname = "127.0.0.1"
}
```

**4. Wire and type identifiers.** Anything appearing on the network or in an actor URL is renamed: the address scheme becomes `pekko://` (and `pekko.tcp://`), and types carrying `Akka` in their name become `Pekko` — `AkkaException` becomes `PekkoException`. Default remoting ports also differ between the two projects.

The fourth substitution is the one with operational consequences. Because the rename reaches **the wire protocol and the cluster membership identifiers**, remoting between an Akka node and a Pekko node is not interoperable and the two cannot form or join a single cluster. A single-node service, a stream pipeline, or an HTTP application can therefore be swapped in place; **a running cluster requires either a full-cluster restart window or a bridge between the two systems**. Steps 1 through 3 are amenable to a scripted textual substitution across the source tree, provided the resulting diff is reviewed: an unqualified `akka` → `pekko` replacement rewrites comments, string literals and unrelated identifiers with the same enthusiasm as it rewrites imports.

A safe rehearsal is to apply the substitution to a scratch copy first — for example `find . \( -name '*.scala' -o -name '*.conf' \) -exec sed -i 's/com.typesafe.akka/org.apache.pekko/g; s/akka-actor/pekko-actor/g; s/\bakka\./org.apache.pekko./g' {} +` — then compile and inspect which references were code and which were incidental text before the same script touches the real repository.

## Pitfalls

- **A rolling upgrade of a cluster silently fails to converge.** The renamed address scheme and membership identifiers make Akka and Pekko remoting mutually unintelligible, so a half-migrated cluster forms two disjoint clusters rather than one.
- **Remoting ports differ by default.** Firewall rules, service definitions and health checks pinned to the Akka default reject or miss the Pekko listener until they are updated alongside the configuration.
- **Configuration silently reverts to defaults.** HOCON does not reject unknown keys, so an `akka { ... }` block left in `application.conf` after the library swap is parsed and ignored, and the actor system starts with default settings instead of the intended ones.
- **Blind textual substitution corrupts non-code text.** A global `akka` → `pekko` replacement rewrites log messages, documentation strings, package names of unrelated third-party libraries, and any persisted data referencing Akka class names.
- **pekko-http is versioned independently of Pekko core.** Pinning both modules to the same version number does not resolve; core 1.6.0 and pekko-http 1.4.0 are the concurrent releases as of August 2026.
- **The 2.0.0 line is not a production upgrade.** It is published as milestones aimed at library maintainers; the 1.x line is the release recommended for deployment.
