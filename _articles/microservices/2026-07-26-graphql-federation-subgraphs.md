---
title: "GraphQL federation: one supergraph, many teams' schemas"
date: 2026-07-26
track: microservices
summary: "A single GraphQL gateway re-creates the monolith you just broke apart. Apollo Federation lets each microservice own a slice of one schema — with @key entities, reference resolvers, and a router that composes it all into a supergraph."
reading_time: 5
tags: [graphql, federation, apollo, subgraphs, microservices, schema-design, newman]
sources:
  - title: "Apollo Federation Directives — Apollo GraphQL Docs"
    url: "https://www.apollographql.com/docs/graphos/schema-design/federated-schemas/reference/directives"
  - title: "Supergraph Routing with the GraphOS Router — Apollo GraphQL Docs"
    url: "https://www.apollographql.com/docs/graphos/routing"
  - title: "Schema Composition — Apollo GraphQL Docs"
    url: "https://www.apollographql.com/docs/graphos/schema-design/federated-schemas/composition"
  - title: "apollographql/router releases (latest v2.15.1, Jun 2026)"
    url: "https://github.com/apollographql/router"
  - title: "WunderGraph — 4 ways to stitch, integrate, compose & federate multiple GraphQL APIs"
    url: "https://wundergraph.com/blog/four_ways_to_stitch_integrate_compose_and_federate_multiple_graphql_apis"
---

The BFF article on this site solved a client-shape problem: one API can't serve mobile, web, and third parties well at once, so you give each frontend its own tailored backend. Federation solves a different problem that shows up *behind* that BFF — or instead of it, when GraphQL itself is the shared API. Once several teams own pieces of one domain graph, who owns the GraphQL schema?

## The monolithic gateway problem

The naive fix for "we want one GraphQL API over N microservices" is a single gateway service that imports every type and writes every resolver. It works for a quarter, then:

- Every team's PR touches the same `schema.graphql` and the same deploy pipeline. Newman's core microservices argument — independent deployability — is gone the moment schema changes need a shared release train.
- The gateway team becomes a queue. They didn't build the `Order` type but own its resolver, so every orders-team change routes through them.
- Ownership blurs. When `Product.reviews` breaks, is that the gateway's bug or the reviews service's? Git blame doesn't answer cleanly because the code lives in the wrong repo.

This is the GraphQL-specific version of the "one API to rule them all" problem the BFF pattern names. Federation is the fix when the split isn't by client, but by schema ownership — then recomposed.

## Subgraphs, a router, and one supergraph

Apollo Federation (currently Federation 2, per Apollo's directive reference) splits a single GraphQL schema into **subgraphs** — one GraphQL server per team or bounded context, each exposing only the types and fields it owns. A **router** — Apollo's GraphOS Router, latest release v2.15.1 as of June 2026 — sits in front, holds the composed **supergraph schema**, and at request time plans a query across whichever subgraphs hold the requested fields, executing sub-requests and stitching the response back together. Clients see one schema and one endpoint; they never know how many services answered.

Subgraphs don't call each other for this. The router does the fan-out, the same way a BFF fans out to services — except the plan is generated automatically from the query shape instead of hand-written per endpoint.

## Entities: the `@key` that stitches types across services

The mechanism that makes this work is the **entity** — a type that multiple subgraphs can contribute fields to, identified by a `@key`. A products subgraph might own `Product`'s catalog fields; a reviews subgraph contributes `Product.reviews` without owning the rest of the type:

```graphql
# --- products subgraph ---
type Product @key(fields: "id") {
  id: ID!
  name: String!
  priceCents: Int!
}

# --- reviews subgraph ---
type Product @key(fields: "id") {
  id: ID!                       # required: matches the key
  reviews: [Review!]!
}

type Review {
  id: ID!
  rating: Int!
  body: String!
}
```

When the router needs `Product.reviews` for a product it already fetched from the products subgraph, it sends the reviews subgraph a **representation** — just `{ __typename: "Product", id: "42" }` — and asks it to resolve the rest. Each subgraph implements this with a **reference resolver**:

```javascript
// reviews subgraph, Apollo Server
const resolvers = {
  Product: {
    // called with the representation the router forwards, not a full object
    __resolveReference(reference) {
      return { id: reference.id }; // enough to resolve reviews below
    },
    reviews(product) {
      return reviewsRepo.findByProductId(product.id);
    },
  },
};
```

That `__resolveReference` function is the whole contract: given a key, return (or start resolving) the entity. It's the federation equivalent of a service exposing a lookup-by-ID endpoint — except the router calls it, not another team's code.

## Crossing boundaries: `@external`, `@requires`, `@provides`

Sometimes a resolver needs a field owned by another subgraph to compute its own field — pricing that varies by shipping weight, say, where weight lives in an inventory subgraph:

```graphql
# pricing subgraph
type Product @key(fields: "id") {
  id: ID!
  weightGrams: Int! @external      # not owned here, but needed below
  shippingCents: Int! @requires(fields: "weightGrams")
}
```

`@external` declares "this field exists on the type but another subgraph resolves it"; `@requires` tells the router to fetch `weightGrams` first and pass it to this resolver before computing `shippingCents`. The inverse, `@provides`, lets a subgraph say "I can resolve this external field myself here," saving a hop — e.g., an orders subgraph that already joined against product name. All three keep field-level ownership honest while still allowing cross-subgraph computation.

## Composition: turning N schemas into one supergraph

Composition is a build step, not a runtime one. A tool — Rover CLI locally, or GraphOS managed federation in CI — takes every subgraph's SDL, checks shared types and `@key`s are compatible, resolves field ownership, and emits one supergraph schema plus a query plan the router understands. Composition fails loudly on conflicts: two subgraphs defining the same non-shareable field, or a `@requires` pointing at a nonexistent field. That failure happens in CI, per subgraph, before deploy — the schema equivalent of a contract-test break, and just as cheap to catch early.

## Team ownership maps directly to the schema

| Layer | Monolithic gateway | Federation |
| --- | --- | --- |
| Schema ownership | One team, one file | One subgraph per team/domain |
| Deploy coupling | Shared release train | Independent per subgraph |
| Cross-team change | PR into shared gateway repo | Add fields to your own subgraph; compose |
| Failure isolation | Gateway resolver bug affects all | Bad subgraph fails its fields only |
| Client view | One schema (same either way) | One schema (composed, not hand-merged) |

Conway's Law stops fighting the architecture: `Product` isn't the gateway team's problem to arbitrate, it's several subgraphs each contributing the slice they own, composed automatically.

## Alternatives worth knowing

Federation isn't the only way to merge GraphQL surfaces. **Schema stitching** — GraphQL's older approach — combines independently-run schemas at a gateway by manually resolving naming collisions and delegating fields; fine for heterogeneous, loosely-related APIs, but it doesn't scale as subgraph count grows, per WunderGraph's comparison. **GraphQL Mesh** wraps non-GraphQL sources (REST, gRPC, SOAP) in a unified GraphQL layer — closer to a gateway integration problem than same-org schema ownership. **WunderGraph Cosmo** offers an open-source, federation-compatible router as an alternative to GraphOS. Teams are also experimenting with **gRPC-based federation**, replacing GraphQL subgraphs with gRPC services behind a federation-aware router, trading per-subgraph flexibility for stricter contracts and lower serialization overhead. Pick federation when one org owns every subgraph; reach for stitching or Mesh when integrating graphs you don't control.

**Try next:** stand up a two-subgraph Federation 2 supergraph locally with `rover dev` (products + reviews from the example above), break composition on purpose by removing the shared `@key`, and watch `rover supergraph compose` fail before you ever start the router.
