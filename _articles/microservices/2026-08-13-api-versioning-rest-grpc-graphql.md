---
title: "API Versioning Three Ways: REST, gRPC, and GraphQL"
date: 2026-08-13
track: microservices
summary: "How each style handles change — URI/header/media-type versioning for REST, backward-compatible field evolution for protobuf, and no-versioning + deprecation for GraphQL — plus when to evolve vs break."
reading_time: 6
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

Independent deployability is the whole point of microservices, and it dies the moment a producer can't change its contract without a lockstep client release. Every API style answers the same question — *how do I evolve without breaking consumers?* — but they answer it very differently. Here's the decision map.

## REST: pick where the version lives

REST has no built-in versioning, so you choose a *carrier* for the version token. Three options dominate:

| Approach | Example | Pros | Cons |
|---|---|---|---|
| **URI path** | `GET /v2/orders/42` | Trivially visible, cacheable, easy to route | Pollutes URLs; a "resource" now has N addresses; encourages big-bang v-bumps |
| **Custom header** | `X-API-Version: 2` or `Accept-Version: 2` | Clean URLs; one canonical resource identity | Invisible in a browser/curl by default; caches must vary on the header |
| **Media type** | `Accept: application/vnd.acme.order.v2+json` | Purest HATEOAS/content negotiation; versions *representations*, not resources | Verbose; poor tooling support; confuses most teams |

A pragmatic fourth option is **date-based versioning in a header**, which GitHub uses in production: clients send `X-GitHub-Api-Version: 2022-11-28` and the server maps the date to a behavior set. This decouples "which contract" from "how many times we've rewritten it."

Whatever the carrier, the rule is: **add fields freely, never repurpose or remove them within a version.** Reserve a new major version for genuinely breaking changes and run old + new in parallel during migration.

## gRPC/protobuf: evolve the schema, not the endpoint

Protobuf is designed so you rarely need a new API version at all — you evolve the message in place. The wire format keys every field by its **field number (tag)**, and the compatibility rules follow from that:

- **Field numbers are permanent.** Changing a field's number is equivalent to deleting and recreating it — always breaking.
- **Never reuse a tag number.** Reusing it makes old bytes decode into the wrong field: data corruption, not a clean error.
- **When you delete a field, `reserve` its number *and* name** so no future edit accidentally reuses them.
- **Adding fields is safe** — old code ignores unknown fields and preserves them on re-serialization.
- A handful of type swaps are wire-compatible (e.g. `int32`↔`int64`↔`bool`, `string`↔`bytes` for valid UTF-8), but they can be lossy — treat with care.

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

Convention still puts a version in the package (`orders.v1`), but that ceremony is for a *breaking* redesign — the day-to-day is additive evolution with `reserved` guarding the graveyard.

## GraphQL: don't version, deprecate

GraphQL's official guidance is explicit: **favor continuous evolution over versioning.** Because clients request exactly the fields they name, adding a field never breaks an existing query. There's no `/v2` — one schema serves everyone.

The safety line is the same as protobuf's: additive is free, subtractive is breaking. Removing/renaming a field, changing its type, or making a nullable field non-null (or an optional arg required) all break clients. When you must retire something, mark it with `@deprecated` and delegate to the replacement:

```graphql
type User {
  id: ID!
  name: String! @deprecated(reason: "Use firstName + lastName; removal after 2026-12-01")
  firstName: String!
  lastName: String!
}
```

Then instrument: tools like Apollo track *which* clients still hit deprecated fields, so you remove only after real usage drops to zero — not on a guessed timeline.

## When to break vs evolve

Break only when the change is semantically impossible to make additively (a field's *meaning* flips, or a required-shape change). Otherwise evolve. The migration playbook is identical everywhere: ship the new shape alongside the old, deprecate with a dated removal window, measure real consumer traffic, and delete only after it hits zero. Versioning is the expensive fallback; backward-compatible evolution is the default.

**Try next:** Take a running protobuf service, delete a field *without* a `reserved` entry, add a new field that reuses the old tag number, and send a message serialized by the old client to the new server. Watch it silently decode into the wrong field — then add `reserved` and confirm the collision is now a compile-time error.
