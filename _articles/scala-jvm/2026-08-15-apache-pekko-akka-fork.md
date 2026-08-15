---
title: "Apache Pekko: The Akka Fork, and Porting an Actor System Off the BSL"
date: 2026-08-15
track: scala-jvm
summary: "In September 2022 Lightbend relicensed Akka under the Business Source License, so the community forked the last Apache-2.0 Akka 2.6.x into Apache Pekko. This walks the fork's story, what Pekko ships (actors, streams, HTTP, cluster), and the mechanical migration — dependency, import, and config renames — with a Scala 3 typed actor and current versions as of August 2026 (Pekko core 1.6.0)."
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

On 7 September 2022, Lightbend announced that Akka — the actor toolkit half of the Scala ecosystem had built distributed systems on for a decade — would move from Apache-2.0 to the **Business Source License (BSL) 1.1**. The BSL is source-available, not open source: production use above a revenue threshold requires a commercial license, and each release converts to Apache-2.0 only after a three-year delay. The change applied going forward, from Akka 2.7 onward. The last Apache-2.0 release, **Akka 2.6.20**, stayed exactly where it was — freely licensed, and frozen.

That frozen artifact became the seed. Volunteers donated it to the Apache Software Foundation, and **Apache Pekko** is the hard fork of Akka 2.6.x, carrying the Apache-2.0 license forward. If your service was on Akka 2.6 and you did not want a per-core commercial bill or a rewrite onto a different concurrency model, Pekko is the drop-in path: same design, same behavior, different namespace.

## What Pekko is, and what it ships

Pekko is not a reimplementation — it is Akka 2.6.x with everything renamed and then maintained independently under Apache governance. The full module set carried over:

- **pekko-actor** / **pekko-actor-typed** — the classic and typed actor runtimes.
- **pekko-stream** — Reactive Streams with back-pressure.
- **pekko-http** — the HTTP server/client stack (formerly akka-http).
- **pekko-cluster**, **pekko-cluster-sharding**, **pekko-persistence**, **pekko-projection** — the distributed and event-sourcing pieces.
- **pekko-connectors** — the integration library formerly known as Alpakka.

As of August 2026 the current line is **Pekko core 1.6.0** (released 17 April 2026), with **pekko-http 1.4.0** (13 July 2026) versioned separately as it always was. Pekko builds cross-publish for **Scala 2.12, 2.13, and 3** (3.3 LTS and newer). A 1.x release is the production recommendation; a 2.0.0 line exists only as milestones for library maintainers. Because Pekko started from a stable, battle-tested codebase rather than a green field, the 1.x releases have been mostly dependency refreshes, JDK-compatibility work, and bug fixes — dull in the best way.

A minimal Scala 3 typed actor looks like ordinary Akka Typed, only the imports moved:

```scala
//> using scala 3.3.6
//> using dep org.apache.pekko::pekko-actor-typed:1.6.0

import org.apache.pekko.actor.typed.*
import org.apache.pekko.actor.typed.scaladsl.Behaviors

object Counter:
  sealed trait Command
  case object Increment                         extends Command
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

State lives in the returned `Behavior` rather than a mutable field, messages are an explicit sealed protocol, and the `ActorSystem` is itself a typed `ActorRef` to the guardian. None of that is Pekko-specific — it is Akka Typed, which is the point.

## The migration is a rename, not a rewrite

Because Pekko is a fork of the exact code you were running, porting an Akka 2.6.x project is mechanical. Four substitutions cover almost everything.

**1. Dependencies** — change the groupId `com.typesafe.akka` to `org.apache.pekko`, and the `akka-` artifact prefix to `pekko-`:

```scala
// before (Akka 2.6.20, Apache-2.0 but frozen)
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

**2. Imports** — the whole package tree moved from `akka.*` to `org.apache.pekko.*`:

```scala
- import akka.actor.typed.*
- import akka.stream.scaladsl.*
+ import org.apache.pekko.actor.typed.*
+ import org.apache.pekko.stream.scaladsl.*
```

**3. Configuration** — the HOCON prefix changes from `akka` to `pekko`:

```hocon
# application.conf
pekko {
  actor.provider = cluster
  remote.artery.canonical.port = 25520
}
```

**4. Wire identifiers** — anything that appears on the network or in a URL: the address scheme becomes `pekko://` (and `pekko.tcp://`), and classes with `Akka` in the name become `Pekko` (`AkkaException` → `PekkoException`). Default remoting ports also differ, which matters most for rolling upgrades.

That last point is the real caveat, so end on it honestly: the rename touches the **wire protocol and cluster membership identifiers**, so an Akka node and a Pekko node cannot form or join the same cluster, and their remoting is not interoperable. A live cluster migration is therefore not a rolling in-place swap — you either take a full-cluster restart window, or run a bridge. For a single-node service or a stream/HTTP app the swap is trivial; for a running distributed cluster, plan the cutover. Most of steps 1–3 can be automated with a scripted find-and-replace across the source tree, but review the diff — a blind `akka`→`pekko` substitution will happily mangle comments, string literals, and unrelated identifiers.

**Try next:** In a scratch copy of an Akka 2.6.x service, run `find . \( -name '*.scala' -o -name '*.conf' \) -exec sed -i 's/com.typesafe.akka/org.apache.pekko/g; s/akka-actor/pekko-actor/g; s/\bakka\./org.apache.pekko./g' {} +`, then compile and eyeball the diff to see exactly which references were code versus incidental text before you trust it on the real repo.
