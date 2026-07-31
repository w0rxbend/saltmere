---
title: "Connect RPC: gRPC You Can Hit With curl"
date: 2026-07-31
track: microservices
summary: "Connect from Buf keeps the .proto contract and generated stubs but ships a POST-and-JSON protocol you can debug with curl — and the same server still speaks gRPC and gRPC-Web, so browsers connect without an Envoy proxy."
reading_time: 5
tags: [connect-rpc, grpc, buf, rpc, protobuf, microservices]
sources:
  - title: "Connect: A better gRPC (Buf blog)"
    url: "https://buf.build/blog/connect-a-better-grpc"
  - title: "The Connect protocol (spec)"
    url: "https://connectrpc.com/docs/protocol/"
  - title: "Connect for Go — Getting started"
    url: "https://connectrpc.com/docs/go/getting-started/"
  - title: "gRPC compatibility (Connect docs)"
    url: "https://connectrpc.com/docs/go/grpc-compatibility/"
  - title: "gRPC vs Connect-RPC vs tRPC 2026 (APIScout)"
    url: "https://apiscout.dev/guides/grpc-vs-connect-rpc-vs-trpc-2026"
---

Sam Newman's *Building Microservices* treats RPC as a spectrum: you want the strong, evolvable contract that Protobuf gives you, but not the operational friction that classic gRPC drags along — HTTP/2-only transport, binary framing you can't read, and the translating proxy every browser client needs. **Connect**, from the team behind Buf, keeps the first half and deletes the second. You write the same `.proto`, generate stubs with the same tool, and get a server that speaks *three* protocols at once.

## Three protocols, one handler

A Connect server implements gRPC, gRPC-Web, and Connect's own protocol from the same generated handlers — the client picks the wire format via `Content-Type`, and the server routes accordingly. That means an existing `grpcurl` or gRPC client keeps working unchanged, while a browser or a shell script can use the plain-HTTP Connect protocol against the same endpoint. Migration is additive: you don't have to move traffic off gRPC to adopt Connect.

## The Connect protocol vs gRPC and gRPC-Web

The Connect protocol is deliberately boring. For unary calls it's an ordinary HTTP POST to `/<package>.<Service>/<Method>`, with the body being either `application/json` or `application/proto`. There's no binary length-prefix framing wrapping your payload and no dependence on HTTP trailers — the two things that make gRPC hard to inspect and force gRPC-Web to run behind Envoy. Because it avoids trailers, unary Connect works over HTTP/1.1, HTTP/2, or HTTP/3; only streaming needs HTTP/2. Methods marked `idempotency_level = NO_SIDE_EFFECTS` can even be called with GET, so they're cacheable.

Errors are just as legible: a failure returns a non-200 status with a JSON body carrying a Connect error `code` (`not_found`, `unavailable`, …), a message, and optional details. Compare that to reading a gRPC status out of an HTTP/2 trailer frame.

## Calling it with curl

This is the headline feature. The public Eliza demo is a real Connect service — hit it straight from a terminal:

```bash
curl \
  --header "Content-Type: application/json" \
  --data '{"sentence": "I feel happy."}' \
  https://demo.connectrpc.com/connectrpc.eliza.v1.ElizaService/Say
```

No plugins, no `grpcurl`, no reflection dance. The URL *is* the RPC, the request *is* JSON, the response *is* JSON. That single property collapses a lot of debugging: you can reproduce a bug from a bash history line, paste a call into a runbook, or poke a service from a CI step with a tool that's already installed everywhere.

## Code generation with buf

You still define the contract once and generate from it. With the Buf CLI (1.72.0 in 2026), a `buf.gen.yaml` lists the plugins and `buf generate` runs them:

```yaml
version: v2
plugins:
  - remote: buf.build/protocolbuffers/go
    out: gen
  - remote: connectrpc.com/go
    out: gen
```

For TypeScript you'd swap in the `es` and `connect-es` plugins instead. The Go generator (`connect-go`, v1.20.0, now requiring Go 1.25) emits a typed client and a handler constructor per service.

## A Go handler and a browser client

The server side is stdlib `net/http` — no bespoke gRPC server object:

```go
mux := http.NewServeMux()
path, handler := greetv1connect.NewGreetServiceHandler(&greetServer{})
mux.Handle(path, handler)

// Serve HTTP/1.1 and cleartext HTTP/2 from the same mux.
p := new(http.Protocols)
p.SetHTTP1(true)
p.SetUnencryptedHTTP2(true)
http.ListenAndServe("localhost:8080", mux) // wire p via http.Server{Protocols: p}
```

And the browser calls it directly with `connect-es` (2.1.x) — no sidecar, no gateway:

```ts
import { createClient } from "@connectrpc/connect";
import { createConnectTransport } from "@connectrpc/connect-web";
import { GreetService } from "./gen/greet/v1/greet_pb";

const client = createClient(
  GreetService,
  createConnectTransport({ baseUrl: "http://localhost:8080" }),
);
const res = await client.greet({ name: "Jane" });
```

Because the browser speaks the Connect protocol over plain `fetch`, there's no Envoy `grpc-web` filter to deploy and operate.

## Why teams pick it

The recurring reasons in 2026 write-ups: **browser support with zero proxy** (the biggest infra saving over gRPC-Web), **debuggability** (curl, browser devtools, and standard HTTP logs all Just Work on JSON payloads), and **incremental adoption** (the server still answers gRPC clients, so nothing downstream has to change on day one). The trade-off is maturity — `connect-go` and `connect-es` are the polished, production-grade implementations, while Python, Kotlin, and Swift are less mature. If your fleet is Go/TS and any of it touches a browser, that trade skews hard toward Connect.

**Try next:** Run `buf generate` on the `greet.v1` schema, start the Go handler above, then call it two ways from one terminal — once with the `curl` JSON POST pattern shown here, and once with `grpcurl -plaintext localhost:8080 greet.v1.GreetService/Greet` — and confirm the *same* server answers both.
