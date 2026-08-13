---
title: "Tapir: HTTP endpoints as data, and everything derived from them"
date: 2026-08-13
track: scala-jvm
summary: "Tapir describes an HTTP endpoint as an immutable Scala value, then interprets that one value into a Netty or http4s server, an sttp client, and OpenAPI docs — no annotations, no reflection. A complete Scala 3 example with a Netty server and live Swagger UI, runnable with scala-cli."
reading_time: 6
tags: [tapir, openapi, netty, scala-3, http]
sources:
  - title: "Tapir documentation — softwaremill"
    url: "https://tapir.softwaremill.com/en/latest/"
  - title: "softwaremill/tapir — GitHub releases"
    url: "https://github.com/softwaremill/tapir/releases"
  - title: "tapir-core_3 — Maven Central via mvnrepository"
    url: "https://mvnrepository.com/artifact/com.softwaremill.sttp.tapir/tapir-core_3"
  - title: "Tapir tech update — SoftwareMill blog"
    url: "https://softwaremill.com/tapir-tech-update/"
---

Most HTTP frameworks make you describe an endpoint twice: once in code (the handler) and once in metadata (annotations, YAML, a hand-written OpenAPI file) — and the two drift apart the week after launch. **Tapir** (SoftwareMill; 1.13.31 as of August 2026) takes the position that an endpoint description is just data: an immutable Scala value listing inputs, outputs, and errors, with *no* logic attached. From that one value it derives the server route, a type-safe client, and OpenAPI documentation. They cannot drift, because they are the same object.

## An endpoint is a value

The core type is `Endpoint[SECURITY, INPUT, ERROR, OUTPUT, R]`. You build one by chaining combinators, each returning a new immutable value:

```scala
val getPet: PublicEndpoint[Long, String, Pet, Any] =
  endpoint.get
    .in("pets" / path[Long]("id"))
    .out(jsonBody[Pet])
    .errorOut(statusCode(StatusCode.NotFound).and(stringBody))
```

Read it like a sentence: GET `/pets/{id}`, where `{id}` parses as `Long`; success is a JSON `Pet`; failure is a 404 with a plain-text body. The compiler tracks all of it in the type. Because it's a value, everything you already do with values works: put shared prefixes in a `val` and reuse them (`baseEndpoint.in("admin")`), keep endpoints in a module the client team depends on without pulling in server code, write unit tests that inspect them, generate them programmatically.

## One definition, three interpreters

Logic attaches separately — `getPet.serverLogic(...)` pairs the description with a function whose signature the endpoint dictates (`Long => F[Either[String, Pet]]` here). Then interpreters consume the result: server interpreters for Netty, http4s, Vert.x, Play, ZIO HTTP, Armeria, and more; client interpreters for sttp; docs interpreters for OpenAPI and AsyncAPI. Same value, three artifacts.

Here is a complete service — endpoint, Netty server, live Swagger UI — as one file. It uses the Loom-based synchronous Netty backend (direct style, no effect wrapper; needs JDK 21):

```scala
//> using scala 3.8.4
//> using jvm 21
//> using dep com.softwaremill.sttp.tapir::tapir-core:1.13.31
//> using dep com.softwaremill.sttp.tapir::tapir-netty-server-sync:1.13.31
//> using dep com.softwaremill.sttp.tapir::tapir-json-circe:1.13.31
//> using dep com.softwaremill.sttp.tapir::tapir-swagger-ui-bundle:1.13.31

import io.circe.generic.auto.*
import sttp.model.StatusCode
import sttp.shared.Identity
import sttp.tapir.*
import sttp.tapir.generic.auto.*
import sttp.tapir.json.circe.*
import sttp.tapir.server.netty.sync.NettySyncServer
import sttp.tapir.swagger.bundle.SwaggerInterpreter

case class Pet(id: Long, name: String, tag: Option[String])

val getPet =
  endpoint.get
    .in("pets" / path[Long]("id"))
    .out(jsonBody[Pet])
    .errorOut(statusCode(StatusCode.NotFound).and(stringBody))

@main def serve(): Unit =
  val pets = Map(1L -> Pet(1, "Otis", Some("dog")))

  val getPetServer = getPet.serverLogic[Identity] { id =>
    pets.get(id).toRight(s"no pet with id $id")
  }

  val docs = SwaggerInterpreter()
    .fromServerEndpoints[Identity](List(getPetServer), "Pets", "1.0.0")

  NettySyncServer().port(8080)
    .addEndpoints(getPetServer :: docs)
    .startAndWait()
```

Run and poke it:

```sh
scala-cli run PetApi.scala
curl -s localhost:8080/pets/1     # {"id":1,"name":"Otis","tag":"dog"}
curl -si localhost:8080/pets/9    # HTTP/1.1 404 ... no pet with id 9
# http://localhost:8080/docs      -> live Swagger UI
```

Notice what you didn't write: no route table, no JSON-error-to-404 mapping, no OpenAPI YAML. `SwaggerInterpreter` walked the same endpoint values and produced a complete, accurate spec — path parameter types, response schemas, error codes — plus the UI to browse it.

## JSON and clients from the same value

`jsonBody[Pet]` is where two derivations meet: a JSON codec (circe above, via `io.circe.generic.auto.*`) and tapir's own `Schema` (via `sttp.tapir.generic.auto.*`), which feeds the OpenAPI output. To swap circe for **jsoniter-scala** — significantly faster, all codegen at compile time, no runtime reflection — replace the dependency with `tapir-jsoniter-scala`, add jsoniter's macros, and derive a `JsonValueCodec[Pet]`; the endpoint definition doesn't change. That's the interpreter pattern working: serialization is a detail the description doesn't know about.

A client comes from the same value too. From any module that sees `getPet` (server code not required):

```scala
val fetch = SttpClientInterpreter()
  .toQuickClient(getPet, Some(uri"http://localhost:8080"))
val pet: Either[String, Pet] = fetch(1L)
```

The value-ness pays off again for cross-cutting concerns. Authentication lives in the description too — `endpoint.securityIn(auth.bearer[String]())` — so a `secureBase` val can carry the auth input plus its checking logic, and every endpoint built from it inherits both the behavior and the `securitySchemes` section of the OpenAPI spec. And because interpreters are pluggable, tests don't need a running server: interpret the same endpoints with the `tapir-sttp-stub-server` backend and exercise your logic through an in-memory client.

Change the endpoint — rename the path segment, widen the error type — and server, client, and docs all update or fail to compile. Annotation-based stacks (Spring, JAX-RS) can't give you that: annotations aren't first-class values, so you can't abstract over them, compose them, or ask the compiler whether client and server still agree. They're strings checked at runtime, if at all. Endpoints-as-data moves that entire class of drift into the type checker.

Tapir 1.x has been the stable line since late 2022, with the 1.13 series current and releases landing near-weekly — check [GitHub releases](https://github.com/softwaremill/tapir/releases) before pinning. If you're on cats-effect or ZIO rather than direct style, the same endpoints interpret into http4s (`Http4sServerInterpreter`) or ZIO HTTP with `serverLogic` returning your effect type — the definitions are portable across all of them.

**Try next:** add a `POST /pets` endpoint with `.in(jsonBody[Pet])` and an in-memory `TrieMap`, restart, and watch it appear in Swagger UI with a full request schema you never wrote — then call it via `SttpClientInterpreter` from a scala-cli test script.
