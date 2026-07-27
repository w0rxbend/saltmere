---
title: "Service-to-Service with gRPC and Protobuf: A Working Field Guide"
date: 2026-07-27
track: microservices
summary: "Define a .proto, generate stubs, and work through the four RPC types, deadlines and metadata, and the field-number rules that make schema evolution safe — plus where Buf and Connect RPC fit in 2026."
reading_time: 6
tags: [grpc, protobuf, buf, connect-rpc, schema-evolution]
sources:
  - title: "gRPC Core Concepts, Architecture and Lifecycle"
    url: "https://grpc.io/docs/what-is-grpc/core-concepts/"
  - title: "Protocol Buffers — Language Guide (proto3)"
    url: "https://protobuf.dev/programming-guides/proto3/"
  - title: "Protocol Buffers — Version Support"
    url: "https://protobuf.dev/support/version-support/"
  - title: "Buf Docs — Detecting breaking changes"
    url: "https://buf.build/docs/breaking/"
  - title: "Connect RPC — Introduction"
    url: "https://connectrpc.com/docs/introduction/"
---

Sam Newman's *Building Microservices* (2nd ed.) frames inter-service calls around one hard constraint: services deploy independently, so the contract between them has to evolve without a lockstep release. gRPC plus Protocol Buffers is the most common way to get a fast, strongly-typed contract that still tolerates that drift. This is a practical walkthrough of the pieces that actually matter in production.

## The contract: a .proto

Everything starts with a schema file. gRPC uses Protocol Buffers as its interface definition language and its wire format.

```protobuf
syntax = "proto3";

package orders.v1;

service OrderService {
  rpc GetOrder(GetOrderRequest) returns (Order);
  rpc ListOrders(ListOrdersRequest) returns (stream Order);
  rpc ImportOrders(stream Order) returns (ImportSummary);
  rpc Sync(stream OrderEvent) returns (stream OrderEvent);
}

message Order {
  string id = 1;
  string customer_id = 2;
  int64 total_cents = 3;
  // field 4 was `string legacy_status` — removed, see reserved below
  reserved 4;
  reserved "legacy_status";
}
```

Note the `v1` in both the package and (by convention) the directory. Versioning the package is the cheapest insurance you can buy against a future breaking change.

## Generating stubs

You *can* call `protoc` directly, but the 2026 default is the Buf CLI (currently 1.72.0), which wraps generation behind a config file. A `buf.gen.yaml` declares the plugins; `buf generate` runs them:

```bash
buf generate            # reads buf.gen.yaml, writes generated code
buf lint                # style + consistency checks on the .proto
buf breaking --against '.git#branch=main'   # diff against main
```

Under the hood this drives protoc 35.x (grpcio 1.82.1 shipped July 2026 on the Python side), but you rarely touch the toolchain directly anymore.

## The four RPC types

gRPC gives you four call shapes, all declared in the `service` block above:

- **Unary** — `GetOrder`: "the client sends a single request and gets a single response back." This is 90% of real traffic.
- **Server streaming** — `ListOrders`: the client sends one request "and gets a stream to read a sequence of messages back." Good for large result sets or feeds.
- **Client streaming** — `ImportOrders`: the client "writes a sequence of messages and sends them," getting one response. Good for uploads/batches.
- **Bidirectional streaming** — `Sync`: "both sides send a sequence of messages using a read-write stream," independently. Good for chat-like or event-sync workloads.

A minimal Python client for the unary call:

```python
import grpc
from orders.v1 import orders_pb2, orders_pb2_grpc

with grpc.insecure_channel("localhost:50051") as channel:
    stub = orders_pb2_grpc.OrderServiceStub(channel)
    resp = stub.GetOrder(
        orders_pb2.GetOrderRequest(id="ord_123"),
        timeout=2.0,                                  # deadline
        metadata=(("authorization", "Bearer ..."),),  # metadata
    )
    print(resp.total_cents)
```

## Deadlines and metadata

Two things every service call should set. gRPC "allows clients to specify how long they are willing to wait for an RPC to complete before the RPC is terminated with a `DEADLINE_EXCEEDED` error" — that's the `timeout` above. Deadlines propagate across hops, so a caller's budget bounds the whole downstream chain and prevents runaway retries from piling up. Without one, a stuck dependency ties up your goroutines/threads indefinitely.

Metadata is "information about a particular RPC call (such as authentication details) in the form of a list of key-value pairs." Keys are strings; values are strings, or binary if the key ends in `-bin`. This is where auth tokens, trace context, and tenant IDs ride along, out of band from your message body.

## Why field numbers are the whole game

This is the part Newman cares about. Protobuf's compatibility model rests entirely on the field *number*, not the name. The number "cannot be changed once your message type is in use because it identifies the field in the message wire format," and numbers "should never be reused."

Concretely, safe evolution looks like this:

- **Add a field** with a new, never-before-used number. Old readers that don't recognize it don't crash — proto3 "messages preserve unknown fields and include them during parsing and in the serialized output." They pass it through untouched.
- **Remove a field** by deleting it *and* reserving its number and name (`reserved 4; reserved "legacy_status";` above). This stops a teammate from silently reassigning `4` to a new, incompatible field later.
- **Never** change an existing field's number, and never repurpose a reserved one — that is a data-corruption bug, not a compile error.

Because old and new readers agree only on numbers, a server on schema v2 and a client on v1 interoperate cleanly as long as you followed those rules. That is what "independent deployability" actually buys you at the wire level.

## Catching mistakes: buf breaking

Humans forget these rules, so `buf breaking` enforces them. It "compares the current version of your Protobuf schema against a past version and reports any changes that would break clients, servers, or the code generated from those schemas." Run it in CI against `main`. The `WIRE` and `WIRE_JSON` rule categories catch on-the-wire breakage; the stricter `FILE` category (the default) also flags source-code-level breaks in generated code. Reassign field `4` and the build fails before it reaches anyone.

## The Connect option

If gRPC's HTTP/2-only, hard-to-curl nature is friction, **Connect RPC** is a gRPC-compatible alternative worth knowing. It speaks three protocols from the same handlers: gRPC, gRPC-Web (no proxy needed), and its own Connect protocol over HTTP/1.1, HTTP/2, and HTTP/3. Crucially it "supports both JSON- and binary-encoded Protobuf," so a Connect endpoint is a plain HTTP+JSON endpoint you can hit with `curl`. You keep the `.proto` contract and Buf toolchain; you gain browser-friendliness and debuggability. `connect-go` and `connect-es` are stable; Python and Kotlin are in beta.

**Try next:** Write the `orders.v1` `.proto` above, run `buf generate` and `buf lint`, commit, then change field `2`'s number and run `buf breaking --against '.git#branch=main'` — watch it fail, then fix it with a `reserved` instead.
