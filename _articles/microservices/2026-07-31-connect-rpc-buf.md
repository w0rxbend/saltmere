---
title: "Connect RPC: gRPC Reachable With curl"
date: 2026-07-31
track: microservices
summary: "Connect from Buf keeps the .proto contract and generated stubs but ships a POST-and-JSON protocol debuggable with curl — and the same server still speaks gRPC and gRPC-Web, so browsers connect without an Envoy proxy."
reading_time: 6
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

**Gist.** Protocol Buffers (Protobuf) give a remote procedure call (RPC) interface a strong, evolvable contract, but the classic gRPC wire protocol attaches operational cost to it: HTTP/2-only transport, binary length-prefixed framing, and a status code delivered in HTTP trailers, which together mean browsers need a translating proxy and humans need a specialised client. **Connect**, from the team behind Buf, keeps the `.proto` contract and code generation while defining a second wire protocol whose unary form is an ordinary HTTP POST with a JSON or Protobuf body, and serves gRPC, gRPC-Web and Connect from **one set of generated handlers**. The cost is a third protocol to reason about and an implementation maturity gradient: `connect-go` and `connect-es` are the production-grade implementations, other languages less so.

## Three protocols behind one handler

A Connect server dispatches on the request `Content-Type`. The generated handler constructor returns a route path and an `http.Handler`; that handler inspects the incoming media type and decodes the request under whichever of the three protocols it names. **The consequence is that adopting Connect is additive**: an existing gRPC client, or `grpcurl`, continues to work against the same address and the same method path, because gRPC's own `application/grpc` content type still resolves to the same service implementation. No traffic has to be moved off gRPC for a browser client to start using the Connect protocol against the identical endpoint.

The URL shape is shared across all three protocols: `/<package>.<Service>/<Method>`. Routing is therefore static and path-based, and any HTTP-layer component — a reverse proxy, an access log, a rate limiter keyed on path — can distinguish methods without understanding Protobuf.

## What the Connect protocol removes

Two properties of gRPC make it opaque to generic HTTP tooling.

- **Length-prefixed framing.** A gRPC body is a sequence of frames, each preceded by a one-byte compressed flag and a four-byte big-endian length. Even a unary call with a JSON-ish payload is not a body a generic client can read or write directly.
- **Trailers.** The call's outcome — `grpc-status` and `grpc-message` — arrives in HTTP trailers after the body. Trailers are an HTTP/2 feature that browser `fetch` does not expose, which is precisely why gRPC-Web exists and why gRPC-Web deployments place an Envoy `grpc-web` filter in front of the service to translate.

The Connect protocol drops both for unary calls. The request is a POST whose body is the message itself, encoded as `application/json` or `application/proto`; **there is no envelope around it**. The response status is the HTTP status, and a failure returns a non-200 status with a JSON body carrying a Connect error `code` (`not_found`, `unavailable`, and the rest of the gRPC code vocabulary rendered as lower-snake-case strings), a human-readable `message`, and optional `details`.

Because unary calls need no trailers, **unary Connect works over HTTP/1.1 as well as HTTP/2**. Streaming requires HTTP/2. Streaming reintroduces framing under its own content types (`application/connect+proto`, `application/connect+json`), and end-of-stream metadata — including the terminal error, when there is one — travels in a final frame in the body rather than in trailers. That is the invariant worth remembering: **Connect never puts call-outcome information anywhere a plain HTTP client cannot see it.**

A method annotated `idempotency_level = NO_SIDE_EFFECTS` may additionally be invoked with GET, which makes its responses eligible for ordinary HTTP caching by intermediaries that have no notion of RPC at all.

## Calling a Connect service with curl

The public Eliza demo is a live Connect service, reachable from a terminal:

```bash
curl \
  --header "Content-Type: application/json" \
  --data '{"sentence": "I feel happy."}' \
  https://demo.connectrpc.com/connectrpc.eliza.v1.ElizaService/Say
```

There is no plugin, no `grpcurl`, and no server-reflection round trip. **The URL is the method, the request body is the message, the response body is the message.** A reproduction of a bug is therefore a line of shell history; a runbook step is a literal command; a smoke test in continuous integration needs only a tool already present in every image.

## Code generation with buf

The contract is still defined once in `.proto` and compiled by the Buf command-line interface (CLI). A `buf.gen.yaml` names the plugins and `buf generate` executes them:

```yaml
version: v2
plugins:
  - remote: buf.build/protocolbuffers/go
    out: gen
  - remote: connectrpc.com/go
    out: gen
```

Two plugins are required because the responsibilities are split: the first emits the message types, the second emits the service surface. The Go generator (`protoc-gen-connect-go`) emits **one typed client interface and one handler constructor per service**. The TypeScript toolchain divides the work differently across its own generator versions, so the plugin list there is not a line-for-line substitution of the Go one.

## A Go handler and a browser client

The server is standard-library `net/http`. There is no separate gRPC server object holding its own listener:

```go
mux := http.NewServeMux()
path, handler := greetv1connect.NewGreetServiceHandler(&greetServer{})
mux.Handle(path, handler)

// Serve HTTP/1.1 and cleartext HTTP/2 from the same mux.
p := new(http.Protocols)
p.SetHTTP1(true)
p.SetUnencryptedHTTP2(true)

srv := &http.Server{Addr: "localhost:8080", Handler: mux, Protocols: p}
srv.ListenAndServe()
```

Both protocol switches matter. HTTP/1.1 carries unary Connect and unary gRPC-Web; **cleartext HTTP/2 (h2c) is what allows a gRPC client to reach the same mux without terminating Transport Layer Security (TLS) first**, since gRPC clients negotiate HTTP/2 and, without TLS, cannot do so via Application-Layer Protocol Negotiation.

The browser calls the same endpoint through `connect-es`:

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

`createConnectTransport` issues plain `fetch` requests under the Connect protocol, so **no `grpc-web` filter is deployed or operated** in the path between browser and service.

## Reported trade-off

The reasons recurring in 2026 write-ups are browser support with no proxy, which removes the translating proxy a gRPC-Web deployment must run, debuggability, since curl, browser developer tools and standard HTTP access logs all operate on JSON payloads unaided, and incremental adoption, since the server continues to answer gRPC clients. The counterweight is implementation maturity: `connect-go` and `connect-es` are the polished implementations, while Python, Kotlin and Swift are less mature. A fleet that is predominantly Go and TypeScript with a browser client sits where the trade-off favours Connect; a fleet built on the less mature runtimes does not.

## Pitfalls

- **Assuming a middlebox that handles gRPC handles Connect streaming.** Streaming still requires HTTP/2 end to end; a proxy that downgrades to HTTP/1.1 leaves unary calls working and breaks streams only, producing a failure that looks method-specific rather than transport-specific.
- **Serving without cleartext HTTP/2 on a plaintext listener.** Unary Connect and curl succeed over HTTP/1.1, so the service appears healthy while every gRPC client fails to negotiate HTTP/2 — the symptom is a protocol error confined to the gRPC callers.
- **Treating a non-200 status as a transport fault.** Connect encodes application-level failures as HTTP error statuses with a JSON error body; a client layer that retries all non-2xx responses will retry `not_found` and similar terminal outcomes.
- **Reading only the HTTP status on a streaming call.** A stream that begins successfully returns 200 and may still terminate in an error carried in the final frame of the body; code that checks the status alone reports success for a failed call.
- **Adding `idempotency_level = NO_SIDE_EFFECTS` to a method that mutates state.** The annotation makes the method callable by GET and its responses cacheable by intermediaries, so a mutation may be served from cache or repeated by a retrying cache client.
- **Generating with only one plugin.** The message plugin and the Connect plugin emit different halves of the surface; omitting either yields code that references types or constructors that were never generated.
