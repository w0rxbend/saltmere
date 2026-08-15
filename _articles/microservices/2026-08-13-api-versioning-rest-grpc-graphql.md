---
title: "API Versioning Three Ways: REST, gRPC, and GraphQL"
date: 2026-08-13
track: microservices
summary: "How each style handles change — URI/header/media-type versioning for REST, backward-compatible field evolution for protobuf, and no-versioning + deprecation for GraphQL — plus when to evolve vs break."
reading_time: 7
tags: [api-versioning, rest, grpc, protobuf, graphql, schema-evolution]
sources:
  - title: "Protocol Buffers — Language Guide (proto3): Updating a Message Type"
    url: "https://protobuf.dev/programming-guides/proto3/"
  - title: "GraphQL — Schema Change Management (Governance & Versioning)"
    url: "https://graphql.org/learn/governance-versioning/"
  - title: "Apollo GraphQL Docs — Schema Deprecations"
    url: "https://www.apollographql.com/docs/graphos/schema-design/guides/deprecations"
  - title: "Microsoft REST API Guidelines — Versioning"
    url: "https://github.com/microsoft/api-guidelines/blob/vNext/Guidelines.md#12-versioning"
  - title: "GitHub REST API — API Versions (date-based headers)"
    url: "https://docs.github.com/en/rest/about-the-rest-api/api-versions"
---

**Gist.** Independent deployability fails the moment a producer cannot change its contract without a lockstep client release, so every application programming interface (API) style needs a rule for evolving a published shape while old consumers keep running. Representational State Transfer (REST) carries an explicit version token, Protocol Buffers (protobuf) evolves the message in place under field-number stability, and GraphQL declines to version at all and retires fields by deprecation. Each mechanism costs something: REST multiplies the surfaces that must be served in parallel, protobuf makes tag numbers a permanent, unrecoverable allocation, and GraphQL defers all removals until measured consumer traffic reaches zero.

## REST: choosing where the version token lives

REST defines no versioning mechanism, so the version token needs a *carrier*. Three carriers dominate:

| Approach | Example | Pros | Cons |
|---|---|---|---|
| **URI path** | `GET /v2/orders/42` | Visible without inspection, cacheable, easy to route | Pollutes URLs; one resource acquires N addresses; encourages big-bang version bumps |
| **Custom header** | `X-API-Version: 2` or `Accept-Version: 2` | Clean URLs; one canonical resource identity | Not visible in a browser or a default `curl` invocation; caches must vary on the header |
| **Media type** | `Accept: application/vnd.acme.order.v2+json` | Content negotiation in its intended form; versions *representations*, not resources | Verbose; poor tooling support |

The carrier choice has a concrete cache consequence. A path-carried version yields **distinct cache keys for free**, because the Uniform Resource Identifier (URI) itself differs. A header-carried version does not: a shared cache that ignores the header will serve a v1 body to a v2 client. **The header must therefore be listed in `Vary`,** and every intermediary that honours `Vary` then maintains one entry per version per URI — the same fan-out the path carrier makes explicit, moved into the cache.

A fourth carrier is **date-based versioning in a header**, which GitHub uses in production: clients send `X-GitHub-Api-Version: 2022-11-28` and the server maps that date to a behaviour set. The mapping is many-to-one — a date names *which contract*, not how many rewrites preceded it — so the number of live behaviour sets is bounded by the number of published dates rather than by release count.

Whatever the carrier, the invariant is the same: **within a version, fields may be added freely but never repurposed or removed.** Repurposing is the dangerous case, because it produces no error. A client that reads `status` continues to parse successfully and acts on a value that now means something else. A new major version is reserved for changes that violate this invariant, and old and new run in parallel for the migration window.

## gRPC and protobuf: evolve the schema, not the endpoint

The protobuf wire format keys every field by its **field number (tag)**, not by its name. A serialized message is a sequence of tag-and-wire-type keys followed by values; the decoder dispatches on the number alone. The compatibility rules follow directly from that encoding:

- **Field numbers are permanent.** Changing a field's number is equivalent to deleting the field and creating a different one — always breaking.
- **A tag number is never reused.** Reusing it makes bytes written by an older peer decode into the wrong field. When the old and new types share a wire type, there is no error to raise: the value lands silently in the new field. This is data corruption, not a failed parse.
- **Deleting a field requires reserving its number *and* its name,** so a later edit that reuses either is rejected by the compiler rather than discovered in production.
- **Adding a field is safe.** Older code ignores unknown fields and preserves them on re-serialization, so a message may pass through an intermediate old-version service without losing the new field.
- A small set of type changes is wire-compatible — for example `int32`/`int64`/`bool`, and `string`/`bytes` where the bytes are valid UTF-8 — but the change can be lossy, so it is not a free substitution.

```protobuf
message Order {
  string id = 1;
  int64 total_cents = 2;

  // field 3 was `string status` — removed in favor of the enum below
  reserved 3;
  reserved "status";

  OrderState state = 4;   // new field, new tag — old clients ignore it
}
```

Convention still places a version in the package name (`orders.v1`), but that ceremony marks a *breaking* redesign. Routine change is additive, with `reserved` acting as the tombstone that keeps retired numbers out of circulation.

## GraphQL: deprecate rather than version

The GraphQL guidance favours continuous evolution over versioning. The property that makes this workable is that **clients enumerate the fields they want**, so a field added to a type appears in no existing query and cannot change any existing response. There is no `/v2`; a single schema serves every client generation.

The safety line matches protobuf's: additive changes are free, subtractive changes break. Removing or renaming a field, changing its type incompatibly, making a non-null output field nullable, or making an optional argument required all break existing documents. The nullability direction matters: on an output field, tightening `String` to `String!` only strengthens what a client already tolerates, while loosening `String!` to `String` introduces a null the client's generated types never allowed. Retirement is announced in the schema with `@deprecated`, which carries a machine-readable reason and leaves the field executable:

```graphql
type User {
  id: ID!
  name: String! @deprecated(reason: "Use firstName + lastName; removal after 2026-12-01")
  firstName: String!
  lastName: String!
}
```

Deprecation alone removes nothing, so removal has to be gated on observation. Tooling such as Apollo attributes field usage to individual clients, which turns "is this field still in use" into a measurement rather than an estimate; the field is deleted after observed usage reaches zero, not on a scheduled date.

## Breaking versus evolving

A break is warranted only when the change cannot be expressed additively — the *meaning* of an existing field changes, or a required shape changes. Everything else evolves. The migration procedure is identical across the three styles: publish the new shape alongside the old, deprecate with a dated removal window, measure real consumer traffic against the old shape, and delete only once that traffic is zero. Versioning is the fallback whose cost is running two contracts at once; backward-compatible evolution is the default.

### Implementation sketch (Scala)

Date-based negotiation resolves an opaque token to a behaviour set. The load-bearing property is that resolution is **monotone**: an unknown-but-newer date must not silently select newer behaviour, and an unrecognised token must fail rather than default to the latest contract.

```scala
import java.time.LocalDate

enum Behaviour:
  case V2025_01_15, V2026_02_10

/** Published contract dates, ascending. Only these are addressable. */
val published: Vector[(LocalDate, Behaviour)] = Vector(
  LocalDate.parse("2025-01-15") -> Behaviour.V2025_01_15,
  LocalDate.parse("2026-02-10") -> Behaviour.V2026_02_10
)

def resolve(header: Option[String]): Either[String, Behaviour] =
  header match
    // Absent header pins the oldest contract: an old client that never sent
    // one must not be upgraded into newer behaviour by a server deploy.
    case None => Right(published.head._2)
    case Some(raw) =>
      published
        .find((d, _) => d.toString == raw.trim)
        .map((_, b) => b)
        .toRight(s"unknown api version: $raw")

def vary: (String, String) = "Vary" -> "X-Api-Version"
```

The exact-match lookup is deliberate. Selecting "the newest published date not after the requested one" would let a client that guesses a future date drift onto whatever contract ships next, which is the failure the version token exists to prevent.

## Pitfalls

- **A header-carried version without `Vary`.** A shared cache stores one entry per URI and returns a v1 body to a v2 client; the response is well-formed, so no client-side error fires.
- **Reusing a retired protobuf tag whose wire type matches the old field.** Messages from old peers decode into the new field with a plausible value and no parse failure — the corruption surfaces later as bad data, not as an exception.
- **Deleting a protobuf field without `reserved`.** Nothing prevents a future edit from reusing the number or the name, so the collision above becomes possible again at the next schema change.
- **Repurposing the meaning of a REST field inside a version.** Consumers parse successfully and act on the new semantics as if they were the old ones; the break is invisible to schema validation.
- **Relaxing a GraphQL output field from non-null to nullable.** Existing documents stay valid and the schema check reports no removal, but responses may now carry `null` where the client's generated type declares a value; the failure appears in the consumer, not in the API.
- **Removing a deprecated field on the announced date rather than on measured usage.** `@deprecated` is advisory; it does not stop execution, so clients still calling the field break at removal time.
- **Package-level protobuf version bumps used for additive change.** Every consumer must regenerate stubs for a change that would have required no action at all, which reintroduces the lockstep release the versioning scheme exists to avoid.

**Try next:** Take a running protobuf service, delete a field *without* a `reserved` entry, add a new field that reuses the old tag number, and send a message serialized by the old client to the new server. Observe the silent decode into the wrong field — then add `reserved` and confirm the collision becomes a compile-time error.
