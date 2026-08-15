---
title: "REST vs gRPC vs GraphQL: A Decision Guide"
date: 2026-08-13
track: microservices
summary: "A trade-off map for the classic system-design question — latency, streaming, browser support, contracts, and over/under-fetching — with a rule of thumb for east-west, public, and aggregation edges."
reading_time: 7
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

**Gist.** Three families of remote interface — Representational State Transfer (REST), gRPC Remote Procedure Calls (gRPC), and GraphQL — all move structured data between processes, but they differ in wire encoding, contract enforcement, and the shape of traffic they admit. Each buys a property at a stated price: gRPC buys compact binary framing and first-class streaming at the price of an intermediary before a browser can call it; GraphQL buys client-controlled field selection at the price of server-side query planning and the loss of free URL-keyed caching; REST buys universal reach and cacheability at the price of fixed payloads that over- or under-serve the caller. The decision is therefore per edge, not per system.

## The trade-off table

| Dimension | REST | gRPC | GraphQL |
|---|---|---|---|
| Wire format | JavaScript Object Notation (JSON) over HTTP/1.1+ (text) | Protocol Buffers over HTTP/2 (binary) | JSON over HTTP (text) |
| Contract | OpenAPI (optional, bolt-on) | `.proto` (mandatory, code generation) | Schema Definition Language (SDL) schema (mandatory) |
| Latency/throughput | Baseline | Compact encoding plus multiplexed transport | Comparable to REST; query planning adds server cost |
| Streaming | Limited (server-sent events, chunked transfer) | Server, client, and bidirectional | Subscriptions, commonly over WebSocket |
| Browser support | Native | Requires a gRPC-Web proxy; **no client-streaming or bidirectional streaming** | Native |
| Over/under-fetching | Common, payload shape is fixed by the server | Fixed messages | Caller selects fields |
| Typical fit | Public and create-read-update-delete (CRUD) surfaces, cache-friendly | Internal east-west, latency-sensitive | Aggregation edge, backend-for-frontend (BFF) |

## Where the differences come from

**gRPC's advantage is stack-level, and each layer is separable.** Protocol Buffers encode each field against a schema as a numeric tag plus a wire type followed by the value, so field names never appear on the wire; JSON transmits every key on every object. The transport is HTTP/2, which multiplexes many concurrent request/response streams over a single Transmission Control Protocol (TCP) connection and compresses headers, removing the per-request connection and header overhead that HTTP/1.1 imposes (MDN, *Evolution of HTTP*). The `.proto` file is compiled into client and server stubs, so **a field-type mismatch is a compile error on both ends rather than a runtime deserialization surprise**.

The constraint that decides deployment is the browser. Browser JavaScript cannot open a raw gRPC connection, so calls pass through a gRPC-Web proxy such as Envoy, and even with that proxy in place **the gRPC-Web tutorial documents that client-side streaming and bidirectional streaming are unavailable** — only unary calls and server streaming survive the translation. A design that relies on a client-streamed upload therefore cannot be exposed to a browser through gRPC-Web without changing the call shape.

**GraphQL moves payload shaping from server to caller.** A screen that needs one user plus that user's last three orders is expressed as a single query naming exactly those fields, so the response contains neither unrequested fields (over-fetching) nor a shortfall that forces follow-up requests (under-fetching). The mechanism is a resolver tree: the server parses the query into a hierarchy of field resolvers and executes them, parents before children. **The cost is structural, not incidental.** Three consequences follow directly from arbitrary query shape:

- *Caching.* A REST response is keyed by its Uniform Resource Identifier (URI), so any intermediary — proxy, content delivery network (CDN) — can cache it without understanding the payload. A GraphQL request body varies per caller, so intermediaries cannot key on the URL, and caching must move into the server or the client.
- *Rate limiting.* Counting requests is a poor proxy for cost when one request may traverse an arbitrary number of resolvers.
- *N+1 execution.* A resolver written per object issues one downstream call per parent item. Resolving `orders` for a list of *n* users with a per-user resolver produces **1 query for the users plus n queries for their orders**; the fan-out grows with result size, not with query text length.

**REST's advantage is that nothing needs to be taught about it.** Verbs and URIs are the native vocabulary of every proxy, CDN, cache, browser and command-line client. `GET /orders/42` is addressable and cacheable by its URI without additional machinery, which neither of the other two provides at comparable cost.

## One minimal example of each

```bash
# REST — resource + verb, cache-friendly
GET /orders/42            ->  200 {"id":42,"total":1999}
```

```protobuf
// gRPC — typed contract, code generation on both ends
service OrderService {
  rpc GetOrder(GetOrderRequest) returns (Order);
  rpc WatchOrders(WatchRequest) returns (stream Order); // server streaming
}
```

```graphql
# GraphQL — the caller selects exactly the fields required
query { order(id: 42) { total customer { name } } }
```

### Implementation sketch (Scala)

The N+1 resolver problem and its remedy are the load-bearing mechanism behind GraphQL's server-side cost. The remedy is a batching loader: sibling resolvers at the same tree depth enqueue keys instead of issuing calls, and the batch is dispatched once per level.

```scala
import scala.collection.mutable
import scala.concurrent.{Future, Promise, ExecutionContext}

/** Collects keys requested during one execution level, then resolves them
  * with a single downstream call. */
final class BatchLoader[K, V](fetch: Seq[K] => Future[Map[K, V]])(using ExecutionContext):
  private val pending = mutable.LinkedHashMap.empty[K, Promise[Option[V]]]

  def load(key: K): Future[Option[V]] = synchronized:
    pending.getOrElseUpdate(key, Promise[Option[V]]()).future

  /** Called once the current level's resolvers have all enqueued their keys. */
  def dispatch(): Future[Unit] =
    val batch = synchronized:
      val snapshot = pending.toSeq
      pending.clear()
      snapshot
    if batch.isEmpty then Future.unit
    else
      fetch(batch.map(_._1)).map: found =>
        // A key absent from the result must still complete, or the resolver hangs.
        batch.foreach((k, p) => p.success(found.get(k)))

// n user resolvers call load(userId) -> one fetch of n keys, not n fetches.
```

The invariant is that `dispatch` runs after every resolver at the current depth has called `load`, and that **every enqueued promise is completed exactly once, including keys the downstream store did not return** — an uncompleted promise leaves the query hanging until the request deadline expires.

## Selecting by edge

- **Internal, east-west, latency-sensitive** (order service to inventory service): gRPC. Binary framing, streaming and generated stubs apply, and no browser sits in the path.
- **Public interface or straightforward CRUD** consumed by unknown clients: REST. Widest reach, HTTP caching, no client-side toolchain requirement.
- **A user-interface aggregation layer or BFF** fronting many services, particularly mobile clients on high-latency links: GraphQL. Field selection eliminates over- and under-fetching and collapses round trips.

The three are not exclusive. A common composition places gRPC between internal services, a GraphQL or REST gateway at the public edge, and REST for third-party webhooks, matching each hop to the constraint that dominates it — payload size, round-trip count, or reach.

An empirical comparison is straightforward to run: implement the same `GetOrder` operation over REST/JSON, gRPC and GraphQL, then load-test each with `wrk` or `ghz` and record p99 latency and bytes on the wire for a 50-field response of which the caller uses 3 fields. The over-fetch cost of the fixed REST payload against GraphQL's selection, and the encoding-size difference for Protocol Buffers, appear directly in those two measurements.

## Pitfalls

- **A gRPC design that streams from client to server cannot be exposed through gRPC-Web.** The symptom is that a browser upload works in an integration test against the native gRPC server and fails behind the proxy; the cause is that gRPC-Web supports only unary and server-streaming calls.
- **Placing a CDN in front of a GraphQL endpoint yields near-zero hit rate.** The symptom is unchanged origin load after adding the CDN; the cause is that the query lives in the request body, so responses are not keyed by URI.
- **Per-request rate limits under-price expensive GraphQL queries.** The symptom is a single client within its request quota saturating the database; the cause is that one request may expand into an unbounded number of resolver executions.
- **A resolver that fetches per parent object multiplies downstream load with result size.** The symptom is latency that scales with list length rather than with query complexity; the cause is the 1 + n call pattern that batching removes.
- **Changing a field's type, or reusing a retired field number for a different field, breaks already-generated clients.** The symptom is a value decoded as garbage or a decoding failure in a consumer that was never rebuilt; the cause is that the wire format identifies fields by number and wire type, so an old client interprets the new bytes under the old schema. Deleting a field is milder — a consumer that ignores unknown fields keeps decoding, but any consumer whose source references the field fails to recompile.
- **Adding OpenAPI to a REST service does not make the contract enforced.** The symptom is a response that diverges from the published specification without any build failing; the cause is that the specification is a description, not a compile-time artefact, unless validation is wired in explicitly.
