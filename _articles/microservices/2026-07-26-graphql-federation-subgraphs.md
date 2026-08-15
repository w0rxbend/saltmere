---
title: "GraphQL federation: one supergraph, many teams' schemas"
date: 2026-07-26
track: microservices
summary: "A single GraphQL gateway re-creates the monolith the service split was meant to dissolve. Apollo Federation gives each microservice a slice of one schema — @key entities, reference resolvers, and a router that composes them into a supergraph."
reading_time: 6
tags: [graphql, federation, apollo, subgraphs, microservices, schema-design, newman]
sources:
  - title: "Apollo Federation Directives — Apollo GraphQL Docs"
    url: "https://www.apollographql.com/docs/graphos/schema-design/federated-schemas/reference/directives"
  - title: "Supergraph Routing with the GraphOS Router — Apollo GraphQL Docs"
    url: "https://www.apollographql.com/docs/graphos/routing"
  - title: "Schema Composition — Apollo GraphQL Docs"
    url: "https://www.apollographql.com/docs/graphos/schema-design/federated-schemas/composition"
  - title: "apollographql/router — Apollo GraphOS Router source and releases"
    url: "https://github.com/apollographql/router"
  - title: "WunderGraph — 4 ways to stitch, integrate, compose & federate multiple GraphQL APIs"
    url: "https://wundergraph.com/blog/four_ways_to_stitch_integrate_compose_and_federate_multiple_graphql_apis"
---

**Gist.** When several teams own pieces of one domain graph, a single GraphQL schema file makes every schema change a shared-release-train change, which removes the independent deployability that motivated the service split. Apollo Federation partitions the schema into per-team **subgraphs**, marks cross-service types as **entities** identified by a `@key`, and puts a **router** in front that plans and executes each query across the subgraphs holding the requested fields. The cost is a composition build step that can fail, plus one additional network round trip per entity boundary a query crosses.

The backend-for-frontend (BFF) article on this site addresses a client-shape problem: one application programming interface (API) cannot serve mobile, web and third parties equally well, so each frontend receives a tailored backend. Federation addresses a different problem, one that appears *behind* a BFF — or in place of it, when GraphQL is itself the shared API: **who owns the schema** once ownership of the domain is distributed.

## The monolithic gateway problem

The direct implementation of "one GraphQL API over N microservices" is a single gateway service that imports every type and implements every resolver. Three consequences follow from that structure:

- Every team's pull request modifies the same `schema.graphql` and the same deploy pipeline. Newman's core microservices criterion — independent deployability — no longer holds once schema changes require a shared release.
- The gateway team becomes a serialization point. It did not model the `Order` type but owns its resolver, so orders-team changes queue behind gateway-team capacity.
- Ownership is not locatable. When `Product.reviews` returns wrong data, the resolver lives in the gateway repository and the data lives in the reviews service, so revision history does not identify the owner.

Federation applies where the split is by **schema ownership** rather than by client.

## Subgraphs, a router, and one supergraph

Apollo Federation — currently Federation 2, per Apollo's directive reference — divides one GraphQL schema into subgraphs: one GraphQL server per team or bounded context, each exposing only the types and fields it owns. A router — Apollo's GraphOS Router — holds the composed **supergraph schema** and, at request time, builds a **query plan**: an ordered set of sub-requests to the subgraphs that hold the requested fields, whose responses it merges into a single result. Clients observe one schema and one endpoint.

**Subgraphs do not call one another to satisfy a federated query.** The fan-out belongs to the router, as in a BFF, with the difference that the plan is derived from the query shape rather than written by hand per endpoint.

## Entities: the `@key` that stitches types across services

The mechanism is the **entity**: a type to which multiple subgraphs contribute fields, identified by a `@key`. A products subgraph owns the catalog fields of `Product`; a reviews subgraph contributes `Product.reviews` without owning the remainder of the type.

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

When the plan requires `Product.reviews` for a product already fetched from the products subgraph, the router sends the reviews subgraph a **representation** — the `__typename` plus the key fields, `{ __typename: "Product", id: "42" }` — and requests the remaining fields. Each subgraph resolves representations through a **reference resolver**:

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

`__resolveReference` is the entire contract: **given the key fields, produce the entity**. The invariant that makes it sound is that the key fields are declared by every subgraph contributing to the type, so any subgraph can identify an entity it does not own.

## Crossing boundaries: `@external`, `@requires`, `@provides`

A resolver sometimes needs a field owned elsewhere to compute its own — shipping cost derived from a weight held in an inventory subgraph:

```graphql
# pricing subgraph
type Product @key(fields: "id") {
  id: ID!
  weightGrams: Int! @external      # not owned here, but needed below
  shippingCents: Int! @requires(fields: "weightGrams")
}
```

`@external` declares that the field exists on the type but is resolved by another subgraph. `@requires` orders the plan: **the router must fetch `weightGrams` first and pass it in the representation** before the pricing subgraph can compute `shippingCents`, which makes the dependency a sequential step rather than a parallel one. `@provides` states the inverse — that a subgraph can resolve a named external field at a particular position, allowing the router to omit a hop, as with an orders subgraph that already carries the product name. All three keep field-level ownership explicit while permitting cross-subgraph computation.

### Implementation sketch (Scala)

The load-bearing step in the router is not the parse but the grouping: representations for the same subgraph and type are collected so a single `_entities` request covers them all, rather than one request per object.

```scala
final case class Representation(typename: String, key: Map[String, String])
final case class Fetch(subgraph: String, typename: String,
                       fields: List[String], reps: List[Representation])

/** Field ownership from the composed supergraph: (type, field) -> subgraph. */
type Owner = Map[(String, String), String]

def planEntityFetches(
    owner: Owner,
    parents: List[Representation],   // entities already resolved upstream
    requested: List[String]          // fields still to resolve on them
): List[Fetch] =
  parents
    .groupBy(_.typename)
    .toList
    .flatMap { (typename, reps) =>
      requested
        .groupBy(f => owner.getOrElse((typename, f), ""))
        .collect { case (sub, fields) if sub.nonEmpty =>
          // one batched request per (subgraph, type), not per entity
          Fetch(sub, typename, fields.sorted, reps.distinct)
        }
    }
```

The `distinct` matters: a list query returning duplicate keys otherwise multiplies the downstream fetch. Batching bounds the fetch count by the number of distinct (subgraph, type) pairs in the plan rather than by the result size.

## Composition: turning N schemas into one supergraph

Composition is a build step, not a runtime one. A tool — the Rover command-line interface (CLI) locally, or GraphOS managed federation in continuous integration (CI) — reads every subgraph's schema definition language (SDL), checks that shared types and `@key`s are compatible, resolves field ownership, and emits one supergraph schema the router consumes. **Composition fails on conflict**: two subgraphs defining the same non-shareable field, or a `@requires` naming a field that does not exist. The failure occurs in CI, per subgraph, before deployment — the schema analogue of a contract-test break.

## Team ownership maps directly to the schema

| Layer | Monolithic gateway | Federation |
| --- | --- | --- |
| Schema ownership | One team, one file | One subgraph per team/domain |
| Deploy coupling | Shared release train | Independent per subgraph |
| Cross-team change | Pull request into shared gateway repo | Fields added to the owning subgraph; compose |
| Failure isolation | Gateway resolver bug affects all | A failing subgraph affects its fields only |
| Client view | One schema | One schema (composed, not hand-merged) |

Arbitration over `Product` disappears: several subgraphs each contribute the slice they own, and composition merges them.

## Alternatives

**Schema stitching**, GraphQL's older approach, combines independently run schemas at a gateway by manually resolving naming collisions and delegating fields; per WunderGraph's comparison it does not scale as subgraph count grows. **GraphQL Mesh** wraps non-GraphQL sources — representational state transfer (REST), gRPC, SOAP — in a unified GraphQL layer, which is an integration problem rather than a same-organisation ownership problem. **WunderGraph Cosmo** provides an open-source, federation-compatible router as an alternative to GraphOS. Federation fits where one organisation owns every subgraph; stitching and Mesh fit graphs the organisation does not control.

## Pitfalls

- A `@requires` on a hot field serialises the plan: the dependent subgraph cannot be queried until the owning subgraph responds, so latency becomes the sum of two round trips rather than the maximum.
- Omitting a key field from a contributing subgraph's type definition fails composition: a subgraph that contributes to an entity must declare the key fields it will be sent in the representation.
- Duplicate keys in a list result produce duplicate representations; without deduplication the entity fetch grows with result size instead of with distinct entity count.
- A field defined in two subgraphs without a shareability declaration is a composition error, not a last-write-wins merge — no new supergraph is emitted, so a router already running continues on the last successfully composed schema.
