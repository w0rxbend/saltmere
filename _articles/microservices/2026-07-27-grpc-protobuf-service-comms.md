---
title: "Service-to-Service with gRPC and Protobuf: A Field Guide"
date: 2026-07-27
track: microservices
summary: "How a .proto contract, the four RPC shapes, deadlines and metadata, and the field-number rules combine to let two services evolve independently — and where Buf and Connect RPC fit."
reading_time: 7
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

**Gist.** Independently deployable services are, by construction, never on the same schema version at the same instant, so the contract between them must tolerate version skew rather than assume a lockstep release. gRPC with Protocol Buffers supplies that contract: a `.proto` interface definition language (IDL) file generates client and server stubs, and Protobuf's compatibility model — identity by field *number*, unknown fields preserved on parse — lets a v1 reader and a v2 writer interoperate. The cost is a build-time code-generation step, a schema artefact that must be governed like a database migration, and a wire format that is opaque to ordinary HyperText Transfer Protocol (HTTP) tooling.

## The contract: a .proto

The schema file is both the interface definition language and the description of the wire format.

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

The `v1` appears in the package name and, by convention, in the directory path. **Versioning the package makes an eventual incompatible change expressible as a new package rather than as a mutation of an existing one**, which is the only route that leaves the old readers working.

## Generating stubs

`protoc` can be invoked directly; the Buf command-line interface (CLI) wraps generation behind a configuration file instead. A `buf.gen.yaml` declares the plugins, and the CLI drives generation, linting and the breaking-change check:

```bash
buf generate            # reads buf.gen.yaml, writes generated code
buf lint                # style + consistency checks on the .proto
buf breaking --against '.git#branch=main'   # diff against main
```

Protobuf's own version-support policy governs how long a given generated-code runtime remains supported; pinning the generator and the runtime together is what keeps generated stubs and the library they link against compatible.

## The four RPC shapes

The `service` block above declares one of each shape defined by the gRPC core-concepts documentation:

- **Unary** — `GetOrder`: "the client sends a single request and gets a single response back."
- **Server streaming** — `ListOrders`: the client sends one request "and gets a stream to read a sequence of messages back." Applicable to large result sets and feeds.
- **Client streaming** — `ImportOrders`: the client "writes a sequence of messages and sends them," receiving one response. Applicable to uploads and batches.
- **Bidirectional streaming** — `Sync`: "both sides send a sequence of messages using a read-write stream," independently — the two streams are not lockstep, so one side may send several messages before the other replies.

A minimal Python client for the unary call, with both of the per-call controls set:

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

gRPC "allows clients to specify how long they are willing to wait for an RPC to complete before the RPC is terminated with a `DEADLINE_EXCEEDED` error". A deadline is per-call, and **it bounds a whole chain only if each service derives the deadline it passes downstream from the one it received** rather than starting a fresh independent timeout at every hop; some gRPC implementations carry that budget automatically through their request-context type, others leave the propagation to the application. With no deadline set, a dependency that never answers holds the caller's thread or coroutine for as long as the connection survives.

Metadata is "information about a particular RPC call (such as authentication details) in the form of a list of key-value pairs". Keys are strings; values are strings, or binary when the key ends in `-bin`. Authentication tokens, trace context and tenant identifiers travel here, out of band from the message body — which is why adding one of them does not touch the `.proto` at all.

### Implementation sketch (Scala)

The propagation rule reduces to arithmetic on a single absolute instant carried through the call chain. Each hop converts the remaining budget into the per-call timeout it passes on, subtracting whatever it spent locally.

```scala
import java.time.Instant

/** The absolute instant at which the whole chain must be abandoned. */
final case class Deadline(at: Instant):
  def remaining(now: Instant): Long =
    java.time.Duration.between(now, at).toMillis

  def expired(now: Instant): Boolean = remaining(now) <= 0

object Deadline:
  /** A caller with no inherited budget starts one; a callee inherits. */
  def in(millis: Long, now: Instant): Deadline =
    Deadline(now.plusMillis(millis))

def callDownstream[A](d: Deadline, now: Instant)(
    rpc: Long => A
): Either[String, A] =
  val budget = d.remaining(now)
  if budget <= 0 then Left("DEADLINE_EXCEEDED")   // fail before dialling
  else
    // Reserve headroom so the caller can still emit its own error response
    // instead of being cut off by its own caller's deadline.
    Right(rpc(math.max(1, budget - 20)))
```

The load-bearing property is that **the instant, not the duration, is what crosses the hop**: a duration restarted at every service multiplies, whereas a shared instant makes the chain's total latency bound equal to the first caller's budget.

## Why field numbers carry the compatibility guarantee

Protobuf's compatibility model rests on the field *number*, not the field name. The proto3 language guide states that a number cannot be changed once the message type is in use, because the number is what identifies the field in the wire format, and that numbers should never be reused. Safe evolution therefore has three rules:

- **Adding a field** requires a new, never-previously-used number. A reader compiled against the older schema does not fail: proto3 "messages preserve unknown fields and include them during parsing and in the serialized output", so the field survives a parse-and-reserialise round trip through an intermediary that has never heard of it.
- **Removing a field** means deleting the declaration *and* reserving both its number and its name (`reserved 4; reserved "legacy_status";`). The reservation is what prevents a later author from assigning `4` to an incompatible type.
- **Renumbering an existing field, or reusing a reserved number**, produces silent misinterpretation on the wire rather than a compile error, because the receiver decodes the incoming bytes under the new field's type.

Because both sides agree only on numbers, a server on schema v2 and a client on v1 interoperate provided those rules hold. That property is what independent deployability amounts to at the wire level.

## Enforcement: buf breaking

Rules that depend on human memory fail eventually, so `buf breaking` checks them mechanically. Buf documents it as comparing the current version of a Protobuf schema against a past version and reporting any changes that would break clients, servers, or the code generated from those schemas. The `WIRE` and `WIRE_JSON` rule categories cover on-the-wire breakage; the stricter `FILE` category, which is the default, additionally flags source-level breaks in the generated code — a renamed message that changes a generated class name breaks compilation without changing a single byte on the wire.

## The Connect option

gRPC requires HTTP/2 and a binary body, which puts it out of reach of ordinary command-line HTTP clients. **Connect RPC** serves three protocols from the same handlers: gRPC, gRPC-Web without a translating proxy, and its own Connect protocol, which works over HTTP/1.1 as well as HTTP/2. It "supports both JSON- and binary-encoded Protobuf", so the same endpoint answers a `curl` request carrying JavaScript Object Notation (JSON). The `.proto` contract and the Buf toolchain are unchanged. Implementation maturity differs by language, so the per-language status is worth checking before adopting it outside Go and TypeScript.

## Pitfalls

- **A field number reassigned after deletion decodes old bytes as the new field.** Nothing rejects the message: the receiver reads the tag, finds the new declaration, and interprets the bytes under the new type. Reserving the number and the name is the only defence.
- **A timeout re-derived at each hop multiplies the chain's worst case.** A three-hop chain where every service sets its own two-second timeout tolerates six seconds, not two. Propagating the absolute deadline keeps the bound at the original budget.
- **Omitting a deadline entirely converts a slow dependency into exhausted caller capacity.** The call is terminated only when the connection is, so in-flight requests accumulate against a dependency that is degraded rather than down.
- **`buf breaking` run against the wrong base passes trivially.** Comparing the branch against itself, or against a tag that already contains the change, reports no differences; the check must diff against the branch the change will merge into.
- **A renamed message passes the `WIRE` categories and breaks the build downstream.** Wire compatibility and generated-code compatibility are different properties, which is why the default `FILE` category is stricter.
- **Unknown-field preservation does not extend to a field whose type changed.** Preservation applies to fields the reader has never seen; a number it does know, carrying bytes written under a different type, is decoded, not preserved.
