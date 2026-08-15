---
title: "API Versioning: URI, Header, and the Expand-Contract Migration"
date: 2026-08-14
track: microservices
summary: "Three ways to version an HTTP API — URI path, custom header, and content negotiation — plus the expand-and-contract (parallel change) pattern that renames a field without breaking a client or forcing a lockstep release."
reading_time: 7
tags: [api-design, versioning, microservices, backward-compatibility, rest]
sources:
  - title: "Newman, S. — Building Microservices, 2nd Edition (schemas, contracts, avoiding lockstep)"
    url: "https://samnewman.io/books/building_microservices_2nd_edition/"
  - title: "Fowler/Sato — Parallel Change (expand and contract)"
    url: "https://martinfowler.com/bliki/ParallelChange.html"
  - title: "Fowler — Tolerant Reader (Postel's Law for consumers)"
    url: "https://martinfowler.com/bliki/TolerantReader.html"
  - title: "Google API Improvement Proposals — AIP-185: Versioning"
    url: "https://google.aip.dev/185"
---

**Gist.** Once a service has a consumer written by someone else, its hypertext transfer protocol (HTTP) interface is a contract, and an incompatible edit to that contract forces a **lockstep release**: client and server must deploy in the same window. Two mechanisms remove that constraint — an explicit version marker (path segment or media type) for changes that genuinely cannot be made compatible, and the **parallel change** pattern (also called expand-and-contract) for everything else, which keeps old and new shapes live at the same time. The cost of the second mechanism is a period during which the provider maintains two representations of the same data and must keep them consistent on every write path.

## Where the version marker goes

**Uniform resource identifier (URI) path** — `GET /v2/orders/42`. The version is visible in access logs, is part of the cache key without further configuration, and can be routed at a gateway by prefix match alone. Google's AIP-185 describes this form: a single **major** version as the first path segment (`v1`, `v2`), with **no minor or patch component in the URL**. The structural objection is that one logical resource then has two identifiers.

**Content negotiation via a media type** — the identifier stays fixed and the representation is selected by the request header:

```http
GET /orders/42 HTTP/1.1
Accept: application/vnd.acme.order.v2+json
```

Resource identity remains single-valued. Two costs follow. The version no longer appears in a URL or in a log line that records only the path, and **any cache in front of the service must vary on `Accept`**, otherwise a `v1` response can be served to a `v2` request from the same cache entry.

**Versioning of the contract rather than the transport.** Independently of where the marker lives, the classification is the semantic-versioning one: additive, backward-compatible changes are minor and require no new endpoint; incompatible changes are major and do. The difficulty is the classification itself, not the notation.

## Expand and contract

Most changes require no `/v2` if both sides observe **Postel's Law** — be conservative in what is sent, liberal in what is accepted. Fowler's **Tolerant Reader** states the consumer-side half: a consumer reads only the fields it needs and ignores the rest. That property is the precondition for parallel change, because it makes **field addition a non-event for every conforming consumer**. Where it does not hold — a deserializer configured to fail on unknown properties, or a validator that rejects extra keys — addition is itself a breaking change, and the pattern below does not apply.

The pattern has three phases:

1. **Expand** — the new shape is added alongside the old; both are populated.
2. **Migrate** — consumers move across individually, on their own release schedule.
3. **Contract** — once no consumer reads the old shape, the old shape is removed.

The invariant that holds across phases 1 and 2 is that **every response satisfies both the old and the new schema simultaneously**. While that invariant holds, any consumer may be at any point in its own migration and still function; the provider is therefore free to deploy without coordination. Phase 3 breaks the invariant deliberately, which is why it is the only phase that requires evidence — not assumption — that no reader of the old shape remains.

Renaming `name` to `full_name` illustrates the sequence. A direct rename breaks every reader at the instant of deployment.

```json
// Phase 1 — EXPAND: write both, keep them in sync
{
  "id": 42,
  "name": "Ada Lovelace",        // old field, still populated
  "full_name": "Ada Lovelace"    // new field, tolerant readers pick this up
}

// Phase 2 — MIGRATE: consumers switch to full_name at their own pace.
//           The old field is deprecated explicitly:
//   Deprecation: Wed, 01 Apr 2026 00:00:00 GMT
//   Sunset: Wed, 01 Oct 2026 00:00:00 GMT
//   Link: <https://api.acme.dev/docs/order-v2>; rel="sunset"

// Phase 3 — CONTRACT: after the sunset date, "name" is dropped
{
  "id": 42,
  "full_name": "Ada Lovelace"
}
```

The double-write in phase 1 belongs in the serializer, where a single mapping derives both keys from one internal value. Deriving both from **one source of truth rather than two stored columns** is what prevents the two fields from diverging: if each is written independently, any code path that updates one and not the other emits a response that violates the invariant, and the failure surfaces on the consumer that happens to read the stale key.

The `Sunset` HTTP response header (RFC 8594) carries the date after which the resource or field is expected to become unresponsive, and the companion `Deprecation` header (RFC 9745) carries the date the deprecation takes effect — both are date values, not flags. Together they make the retirement schedule machine-readable, so a consumer can detect and alarm on continued use of a deprecated field rather than depending on a notification being read by a person.

### Implementation sketch (Scala)

The load-bearing part is the serializer emitting both shapes from one value, and the reader tolerating either.

```scala
final case class Person(id: Long, fullName: String)

enum Phase:
  case Expand, Contract

// One source of truth, two keys, chosen by migration phase.
def encode(p: Person, phase: Phase): Map[String, Any] =
  val base = Map[String, Any]("id" -> p.id, "full_name" -> p.fullName)
  phase match
    case Phase.Expand   => base + ("name" -> p.fullName) // same field re-read, not a second store
    case Phase.Contract => base

// Tolerant reader: unknown keys ignored, new key preferred, old key accepted.
def decode(m: Map[String, Any]): Option[Person] =
  for
    id   <- m.get("id").collect { case l: Long => l }
    name <- m.get("full_name").orElse(m.get("name")).collect { case s: String => s }
  yield Person(id, name)

// Retirement schedule as headers; both values are HTTP dates.
def deprecationHeaders(deprecatedAt: String, sunset: String): Seq[(String, String)] =
  Seq("Deprecation" -> deprecatedAt, "Sunset" -> sunset)
```

`decode` accepts a phase-1 response, a phase-3 response, and a legacy phase-0 response without branching on a version marker: the union of accepted shapes is what allows consumer and provider deployments to be ordered arbitrarily.

## Operating the transition

- **A field is never removed or repurposed in place.** Removing `name` and later binding the same key to a different meaning produces a silent break: the consumer parses successfully and computes on the wrong value. New keys are added; old keys are retired only after the sunset window.
- **Consumer-driven contract tests supply the evidence for phase 3.** Newman treats these as the mechanism by which a provider learns what its consumers depend on: each consumer publishes the shape it expects, and the provider's continuous-integration run fails when a change would violate a published expectation. The failure occurs at build time rather than in production.
- **A new major version is reserved for changes that cannot be expressed additively** — restructuring a resource, removing an operation, changing a field's type. The old and new major versions then run **simultaneously** for a transition period, so consumers migrate gradually rather than in lockstep; a major version that replaces its predecessor at the instant of deployment reintroduces exactly the lockstep the marker was meant to avoid.

## Pitfalls

- **A cache placed in front of a media-type-versioned endpoint without `Vary: Accept` serves a `v1` body to a `v2` request.** The cache key contains only the path, so the first response stored wins for every subsequent version.
- **A deserializer configured to reject unknown properties turns an additive change into an outage.** The provider adds a field believing it compatible; the consumer fails at parse time on the field it was never meant to read.
- **Two independently written columns behind the old and new field name drift.** Any write path that updates one and misses the other emits a response satisfying neither schema consistently, and the mismatch appears only on the consumer reading the stale key.
- **Contracting before contract-test coverage is complete removes the field for an unobserved consumer.** Absence of traffic in the tested set is not evidence of absence of readers; the break appears when that consumer next runs.
- **A `Sunset` date published without a corresponding gate in the provider's release process lapses without action.** The header records an intention that nothing enforces, so the old field persists and accumulates new readers.
- **Encoding minor or patch numbers in the URI path multiplies routing entries.** Every compatible change creates a new cache key and a new gateway route, neither of which a major-version-only path segment produces.
