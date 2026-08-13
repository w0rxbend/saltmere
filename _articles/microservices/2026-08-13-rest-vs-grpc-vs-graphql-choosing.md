---
title: "REST vs gRPC vs GraphQL: A Decision Guide"
date: 2026-08-13
track: microservices
summary: "A trade-off map for the classic system-design question — latency, streaming, browser support, contracts, and over/under-fetching — with a rule of thumb for east-west, public, and aggregation edges."
reading_time: 6
tags: [rest, grpc, graphql, api-design, system-design, bff]
sources:
  - title: "gRPC — Core Concepts, Architecture and Lifecycle"
    url: "https://grpc.io/docs/what-is-grpc/core-concepts/"
  - title: "gRPC-Web — Basics Tutorial (browser limitations)"
    url: "https://grpc.io/docs/platforms/web/basics/"
  - title: "GraphQL — Introduction to GraphQL"
    url: "https://graphql.org/learn/"
  - title: "MDN — HTTP: Evolution of HTTP (HTTP/2 multiplexing)"
    url: "https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Evolution_of_HTTP"
  - title: "Apollo — What is a supergraph / BFF patterns"
    url: "https://www.apollographql.com/docs/graphos/get-started/concepts/graphs"
---

"REST, gRPC, or GraphQL?" is a staple interview prompt because the right answer is always *"depends on the edge."* All three move structured data over the network; they differ in wire format, contract strength, and traffic shape. This is a decision guide, not a tutorial — pick by the constraint that dominates.

## The trade-off table

| Dimension | REST | gRPC | GraphQL |
|---|---|---|---|
| Wire format | JSON over HTTP/1.1+ (text) | Protobuf over HTTP/2 (binary) | JSON over HTTP (text) |
| Contract | OpenAPI (optional, bolt-on) | `.proto` (mandatory, codegen) | SDL schema (mandatory) |
| Latency/throughput | Baseline | Best — compact + multiplexed | ~REST; query planning adds server cost |
| Streaming | Limited (SSE/chunked) | First-class: server, client, bidi | Subscriptions (usually WebSocket) |
| Browser support | Native | Needs gRPC-Web proxy; **no bidi streaming** | Native |
| Over/under-fetching | Common (fixed payloads) | Fixed messages | Solved — client picks fields |
| Best fit | Public/CRUD, cache-friendly | Internal east-west, low-latency | Aggregation edge / BFF |

## Why the differences exist

**gRPC is fast because of its stack, not magic.** Protobuf serializes to compact binary, and HTTP/2 multiplexes many calls over one connection with header compression. It generates typed client/server stubs from the `.proto`, so the contract is enforced at compile time. The catch: browsers can't speak raw gRPC — you need a gRPC-Web proxy (Envoy), and even then **client-side and bidirectional streaming aren't supported**. That makes gRPC ideal *inside* the mesh (service-to-service, east-west) and awkward at the public edge.

**GraphQL solves the fetching problem.** A mobile screen that needs a user plus their last three orders is one round trip with exactly the requested fields — no over-fetching a fat REST payload, no under-fetching that forces N follow-up calls. That superpower is also its cost: the server does query planning, arbitrary queries make caching and rate-limiting harder, and a naive resolver invites N+1 database hits. It shines as an **aggregation layer / Backend-for-Frontend** over many downstream services.

**REST wins on ubiquity and caching.** Plain HTTP verbs and URLs mean every proxy, CDN, and `curl` already understands it. `GET /orders/42` is cacheable by URL for free — something neither peer gives you cheaply. For public CRUD APIs and anything that benefits from HTTP caching, REST remains the default.

## One tiny example of each

```bash
# REST — resource + verb, cache-friendly
GET /orders/42            ->  200 {"id":42,"total":1999}
```

```protobuf
// gRPC — typed contract, codegen both ends
service OrderService {
  rpc GetOrder(GetOrderRequest) returns (Order);
  rpc WatchOrders(WatchRequest) returns (stream Order); // server streaming
}
```

```graphql
# GraphQL — client selects exactly the fields it needs
query { order(id: 42) { total customer { name } } }
```

## Picking by edge

- **Internal, east-west, latency-sensitive** (order service → inventory service): **gRPC.** Binary framing, streaming, and generated stubs pay off, and there's no browser in the path.
- **Public API or simple CRUD** consumed by unknown clients: **REST.** Maximum reach, HTTP caching, lowest cognitive load.
- **A UI/BFF aggregating many services**, especially mobile on flaky networks: **GraphQL.** Kill over/under-fetching and round trips.

These aren't exclusive. The common 2026 shape is gRPC between internal services, a GraphQL or REST gateway at the public edge, and REST for third-party webhooks. Match the protocol to the constraint that hurts most on each hop — payload size, round trips, or reach.

**Try next:** Stand up the same `GetOrder` endpoint in all three (REST/JSON, gRPC, GraphQL), then run each under `wrk` or `ghz` and capture p99 latency and bytes-on-wire for a 50-field response where the client needs only 3 fields. The over-fetch tax on REST vs GraphQL — and the binary-size win for gRPC — will be obvious in the numbers.
