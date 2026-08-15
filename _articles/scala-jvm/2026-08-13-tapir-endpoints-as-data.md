---
title: "Tapir: HTTP endpoints as data, and everything derived from them"
date: 2026-08-13
track: scala-jvm
summary: "Tapir describes an HTTP endpoint as an immutable Scala value, then interprets that one value into a Netty or http4s server, an sttp client, and OpenAPI documentation — no annotations, no reflection. A Scala 3 example with a Netty server and live Swagger UI, runnable with scala-cli."
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

**Gist.** Most Hypertext Transfer Protocol (HTTP) frameworks require an endpoint to be described twice — once as handler code and once as metadata (annotations, YAML, a hand-written OpenAPI file) — and the two descriptions drift apart independently. **Tapir** (SoftwareMill) represents an endpoint description as data: an immutable Scala value listing inputs, outputs and errors, carrying **no logic**, from which the server route, a type-safe client and the OpenAPI document are all derived. The cost is that the description must be expressible in Tapir's combinator vocabulary and its types: anything the endpoint type cannot express has to be pushed into the server logic, where the docs and client interpreters can no longer see it.

## An endpoint is a value

The core type is `Endpoint[SECURITY, INPUT, ERROR, OUTPUT, R]`. Its five parameters record, in order, the security input, the ordinary input, the error output, the success output, and the capabilities the endpoint requires (streaming, WebSockets). An endpoint is built by chaining combinators, each returning a **new immutable value** rather than mutating a builder:

```scala
val getPet: PublicEndpoint[Long, String, Pet, Any] =
  endpoint.get
    .in("pets" / path[Long]("id"))
    .out(jsonBody[Pet])
    .errorOut(statusCode(StatusCode.NotFound).and(stringBody))
```

The value reads as a sentence: GET `/pets/{id}`, where `{id}` parses as `Long`; success is a JavaScript Object Notation (JSON) `Pet`; failure is a 404 with a plain-text body. `PublicEndpoint` is the alias for an endpoint whose security input is `Unit`. Each combinator refines the type parameters, so **the input and output types are visible to the compiler at the definition site**, not discovered at request time.

Because the description is an ordinary value, ordinary value operations apply. A shared prefix can live in a `val` and be extended (`baseEndpoint.in("admin")`). Endpoint definitions can be published in a module that the client team depends on **without pulling in any server dependency**, since the server route is produced later by a separate interpreter. Endpoints can be inspected in unit tests, held in collections, and generated programmatically.

## One definition, several interpreters

Logic attaches separately. `getPet.serverLogic(...)` pairs the description with a function **whose signature the endpoint dictates** — here `Long => F[Either[String, Pet]]`, with the `Long` from the input, the `String` from the error output and the `Pet` from the success output. A mismatch is a compile error, not a runtime 500.

Interpreters then consume the resulting value: server interpreters for Netty, http4s, Vert.x, Play, ZIO HTTP and Armeria among others; client interpreters for sttp; documentation interpreters for OpenAPI and AsyncAPI. **The same value produces all three artefacts, so they cannot disagree**; a change to the path segment or a widening of the error type either propagates to server, client and documentation together or fails to compile.

### Implementation sketch (Scala)

A complete service — endpoint, Netty server, live Swagger user interface (UI) — as one file. It uses the Loom-based synchronous Netty backend, which is direct style with no effect wrapper and requires Java Development Kit (JDK) 21:

```scala
//> using scala 3.3.4
//> using jvm 21
//> using dep com.softwaremill.sttp.tapir::tapir-core:1.11.10
//> using dep com.softwaremill.sttp.tapir::tapir-netty-server-sync:1.11.10
//> using dep com.softwaremill.sttp.tapir::tapir-json-circe:1.11.10
//> using dep com.softwaremill.sttp.tapir::tapir-swagger-ui-bundle:1.11.10

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

  // Identity: the effect type of the synchronous backend — no wrapper
  val getPetServer = getPet.serverLogic[Identity] { id =>
    pets.get(id).toRight(s"no pet with id $id")
  }

  val docs = SwaggerInterpreter()
    .fromServerEndpoints[Identity](List(getPetServer), "Pets", "1.0.0")

  NettySyncServer().port(8080)
    .addEndpoints(getPetServer :: docs)
    .startAndWait()
```

Exercising it:

```sh
scala-cli run PetApi.scala
curl -s localhost:8080/pets/1     # {"id":1,"name":"Otis","tag":"dog"}
curl -si localhost:8080/pets/9    # HTTP/1.1 404 ... no pet with id 9
# http://localhost:8080/docs      -> live Swagger UI
```

No route table, no mapping from JSON error to 404, and no OpenAPI YAML appears in the source. `SwaggerInterpreter` traverses the same endpoint values and emits the specification — path parameter types, response schemas, error codes — together with the UI that renders it.

## Codecs, clients and security

`jsonBody[Pet]` is the point where two derivations meet: a JSON codec (circe above, via `io.circe.generic.auto.*`) and Tapir's own `Schema` (via `sttp.tapir.generic.auto.*`), the latter feeding the OpenAPI output. **Both are required**: the codec moves bytes, the schema describes them. Substituting jsoniter-scala, which generates codecs by macro from an explicitly summoned `JsonValueCodec` rather than from an automatic import, means replacing the dependency with `tapir-jsoniter-scala`, adding jsoniter's macros and deriving a `JsonValueCodec[Pet]`; **the endpoint definition is unchanged**, because serialisation is a concern of the interpreter rather than of the description.

A client is derived from the same value, in any module that can see `getPet` and without server code on the classpath:

```scala
val fetch = SttpClientInterpreter()
  .toQuickClient(getPet, Some(uri"http://localhost:8080"))
val pet: Either[String, Pet] = fetch(1L)
```

Authentication also lives in the description. `endpoint.securityIn(auth.bearer[String]())` populates the `SECURITY` type parameter, so a `secureBase` value can carry both the authentication input and the function that checks it; every endpoint derived from that base inherits the check **and** contributes the corresponding `securitySchemes` entry to the OpenAPI document. Because interpretation is pluggable, tests need no listening socket: the same endpoints interpreted with the `tapir-sttp-stub-server` backend are exercised through an in-memory client.

The contrast with annotation-based stacks such as Spring or JAX-RS is structural rather than a matter of degree. Annotations are not first-class values, so they cannot be abstracted over, composed, or passed to a function; agreement between client and server is checked at runtime if at all. Endpoints-as-data relocates that class of drift into the type checker.

Tapir 1.x has been the stable line since 2022, and patch releases are frequent; the [GitHub releases](https://github.com/softwaremill/tapir/releases) page is the reference before pinning a version, and the versions in the sketch below should be checked against it. On cats-effect or ZIO rather than direct style, the same endpoint values interpret into http4s (`Http4sServerInterpreter`) or ZIO HTTP with `serverLogic` returning the corresponding effect type — the definitions are portable across all of them.

## Pitfalls

- **Omitting `sttp.tapir.generic.auto.*` while keeping the circe auto-derivation import** compiles the codec but leaves no `Schema`, so the OpenAPI document lacks the response schema even though requests are served correctly.
- **Deriving both codec and schema automatically for a large case-class graph** moves the cost to compile time; the derivation is per-use-site unless the instances are cached in explicit `given` values.
- **Attaching a shared prefix by copying an endpoint definition rather than extending a base value** removes the compile-time link: renaming the segment in one copy leaves the others serving the old path, which is the drift the design exists to prevent.
- **Encoding a distinction in the server logic that the endpoint type does not express** — for example returning different status codes from a single `String` error output — makes the OpenAPI document and the derived client wrong, because neither interpreter can observe logic.
- **Ordering endpoints so that a broader path matches first** causes the later, more specific endpoint never to be reached; the server interpreter tries the list in the order given.
- **Running the synchronous Netty backend on a JDK older than 21** is unsupported: that module depends on the Loom virtual-thread facilities finalised in JDK 21.
