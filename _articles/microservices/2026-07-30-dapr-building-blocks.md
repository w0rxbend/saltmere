---
title: "Dapr Building Blocks: Swapping Redis for Kafka as a Configuration Change"
date: 2026-07-30
track: microservices
summary: "Dapr runs as a sidecar and exposes pluggable building blocks — state, pub/sub, service invocation, actors, workflow — behind a stable HTTP/gRPC API. The backing infrastructure is wired in component YAML, so switching Redis to Postgres or Kafka is a configuration change rather than a rewrite."
reading_time: 7
tags: [dapr, microservices, sidecar, pubsub, state-management, cncf]
sources:
  - title: "Dapr docs — Building blocks overview"
    url: "https://docs.dapr.io/concepts/building-blocks-concept/"
  - title: "Dapr docs — State management API reference"
    url: "https://docs.dapr.io/reference/api/state_api/"
  - title: "CNCF — Announcing Dapr graduation"
    url: "https://www.cncf.io/announcements/2024/11/12/cloud-native-computing-foundation-announces-dapr-graduation/"
  - title: "Dapr runtime releases (GitHub)"
    url: "https://github.com/dapr/dapr/releases"
  - title: "Dapr docs — Publish & subscribe overview"
    url: "https://docs.dapr.io/developing-applications/building-blocks/pubsub/pubsub-overview/"
---

**Gist.** Microservice glue code — retrying a call, discovering a peer, persisting a key, publishing an event, fetching a secret — is rewritten per language and per client library, so replacing a backing store means editing code in every service that touches it. Dapr (Distributed Application Runtime) moves that glue into a sidecar process that exposes a fixed HTTP/gRPC surface over `localhost`, with the concrete backend named in deployment-time YAML. The cost is an extra process and a network hop on every call, plus the operational surface of the Dapr control plane.

Dapr is a Cloud Native Computing Foundation (CNCF) **graduated** project as of **November 12, 2024**, having entered CNCF incubation in November 2021. The runtime is released from the `dapr/dapr` repository on a 1.x line; the API paths described below have been stable across that line. What follows describes the building-block catalogue and works through the state-management and publish/subscribe APIs concretely.

## The sidecar model

Dapr runs as a separate process beside the application: a sidecar container under Kubernetes, or a local process under `dapr run`. The application process links no Redis client and no Kafka client. It issues HTTP or gRPC requests to the sidecar on `localhost`, and the sidecar performs the corresponding operation against the real infrastructure.

The consequence is a narrowing of what the application knows. It knows that "a state store" and "a pub/sub broker" exist under given names. Which store, which broker, and the credentials required to reach them are declared in **component YAML** deployed alongside the application. **The application binary is invariant under a change of component `spec.type`;** only the sidecar's view of the world changes.

## The building blocks

A building block is a capability exposed at a stable API path (`/v1.0/state`, `/v1.0/publish`, `/v1.0/invoke`, and so on) and backed by a pluggable **component**. The catalogue is:

| Building block | Function |
|---|---|
| Service invocation | Calls another application by identifier, with discovery, mutual TLS, retries, tracing |
| State management | Key/value persistence over pluggable stores |
| Publish & subscribe | At-least-once topic messaging, sender and receiver decoupled |
| Bindings | Bi-directional connectors to external systems (input triggers and output calls) |
| Actors | Virtual actors: single-threaded units of state plus compute |
| Secrets | Reads secrets from vaults, cloud stores, or Kubernetes |
| Configuration | Reads and subscribes to configuration items |
| Distributed lock | Mutual exclusion across application instances |
| Cryptography | Encryption and decryption without exposing keys to the application |
| Jobs | Scheduling of delayed and recurring work |
| Workflow | Durable, crash-resilient orchestration of long-running processes |
| Conversation | A unified API for prompting large language models, with retries and scrubbing of personally identifiable information |

The catalogue itself is not the load-bearing part. **The invariant is that each block hides an interchangeable backend behind an identical call, and that the API contract is held fixed across implementations.**

## State management, concretely

A state store is declared as a component. A Redis store suitable for local development:

```yaml
# components/statestore.yaml
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: statestore
spec:
  type: state.redis
  version: v1
  metadata:
    - name: redisHost
      value: localhost:6379
    - name: redisPassword
      value: ""
```

**The string that API calls reference is `metadata.name` — here `statestore` — not the technology name `redis`.** Conflating the two is the most common first-run error. With the sidecar on port 3500, a key is saved, read and deleted by talking only to `localhost`:

```bash
# Save — the body is a JSON array of {key, value} objects
curl -X POST http://localhost:3500/v1.0/state/statestore \
  -H "Content-Type: application/json" \
  -d '[{ "key": "order-42", "value": { "status": "packed" } }]'

# Read back
curl http://localhost:3500/v1.0/state/statestore/order-42
# -> {"status":"packed"}

# Delete
curl -X DELETE http://localhost:3500/v1.0/state/statestore/order-42
```

The path grammar is the whole contract: `POST /v1.0/state/<store>` writes, `GET /v1.0/state/<store>/<key>` reads, `DELETE /v1.0/state/<store>/<key>` removes. Moving from Redis to PostgreSQL changes `spec.type` to `state.postgresql` and its metadata; the three commands above are untouched.

Optional semantics ride on the same paths. An `etag` supplied in the write body requests optimistic concurrency; the current value is returned as an `ETag` header on read and enforced through `If-Match` on delete. `POST /v1.0/state/<store>/transaction` performs a multi-key atomic write **where the backing store supports transactions** — support is a property of the component, not of the API, so the same request succeeds against one store and is rejected by another.

### Implementation sketch (Scala)

The optimistic-concurrency loop is the mechanism worth making explicit: read the value together with its ETag, compute the successor, and write it back conditioned on that ETag. A concurrent writer that commits first invalidates the tag, the conditional write fails, and the loop re-reads rather than overwriting. Only the JDK HTTP client is used, since the API is plain HTTP.

```scala
final case class Versioned(value: String, etag: Option[String])

def read(store: String, key: String): Versioned =
  val res = http.send(
    HttpRequest.newBuilder(URI.create(s"$base/v1.0/state/$store/$key")).GET().build(),
    HttpResponse.BodyHandlers.ofString()
  )
  Versioned(res.body, res.headers.firstValue("ETag").toScala)

/** With an ETag, writes only if it still matches and returns false on conflict;
  * without one, the write is unconditional and always reports success. */
def write(store: String, key: String, next: String, etag: Option[String]): Boolean =
  val tagField = etag.map(t => ",\"etag\":\"" + t + "\"").getOrElse("")
  val body = s"""[{"key":"$key","value":$next$tagField}]"""
  val res = http.send(
    HttpRequest.newBuilder(URI.create(s"$base/v1.0/state/$store"))
      .header("Content-Type", "application/json")
      .POST(HttpRequest.BodyPublishers.ofString(body)).build(),
    HttpResponse.BodyHandlers.ofString()
  )
  res.statusCode() / 100 == 2

@tailrec
def update(store: String, key: String, f: String => String): String =
  val Versioned(v, tag) = read(store, key)
  val next = f(v)
  // A failed conditional write means another writer won; re-read, never retry blindly.
  // With tag == None nothing conditions the write, so this never loops.
  if write(store, key, next, tag) then next else update(store, key, f)
```

The absent-ETag case is the honest part of the sketch. **ETag support is a component capability; a store that returns no ETag offers no compare-and-set, and the loop degrades to last-writer-wins.**

## Publish and subscribe, concretely

A pub/sub component has the same shape:

```yaml
# components/pubsub.yaml
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: orderpubsub
spec:
  type: pubsub.redis
  version: v1
  metadata:
    - name: redisHost
      value: localhost:6379
```

Publication is a single call to `POST /v1.0/publish/<pubsub-name>/<topic>`:

```bash
curl -X POST http://localhost:3500/v1.0/publish/orderpubsub/orders \
  -H "Content-Type: application/json" \
  -d '{ "orderId": 42, "total": 19.99 }'
```

Dapr wraps the payload in a CloudEvents envelope, delivers it **at least once**, and performs retries. Subscribers register a route either declaratively or by serving a `/dapr/subscribe` endpoint, and the sidecar POSTs each message to that route. Substituting `pubsub.kafka`, Azure Service Bus or RabbitMQ for `pubsub.redis` leaves the publish call and the subscriber routes unchanged.

The delivery guarantee is the detail that survives every substitution. **At-least-once means a subscriber can observe the same message more than once, and the runtime does not suppress duplicates;** idempotence remains the subscriber's obligation regardless of which broker is configured.

## Service invocation

Service-to-service calls go through the same `localhost` surface rather than through hostname resolution and application-side mutual TLS:

```bash
curl http://localhost:3500/v1.0/invoke/checkout/method/health
```

`checkout` is the target application's Dapr app-ID and `health` is the method, which is a path on that application. The sidecar performs discovery, mutual TLS between sidecars, retries, and propagation of distributed-trace context.

## Where the model fits

The model pays where services are polyglot and the cross-cutting concerns would otherwise be reimplemented per language, and where the backing infrastructure is expected to differ between environments — Redis in development, a managed cloud store in production — without code churn. **The stable API contract is the product; components are the swappable part.**

It is not free. Each application runs an additional process, which adds a network hop and a latency increment to every call that would otherwise be an in-process library call, alongside the operational surface of the control plane on Kubernetes. For a single-language service talking to one database it owns outright, a client library is the simpler arrangement. The trade shifts as the number of services, languages and backends grows.

## Pitfalls

- **Calls fail with an unknown-store error although Redis is reachable.** The API path takes `metadata.name` from the component YAML; using the technology name (`redis`) instead of the declared name (`statestore`) addresses a component that does not exist.
- **A transactional write is rejected while single-key writes succeed.** Multi-key atomicity at `/v1.0/state/<store>/transaction` requires the backing store to support transactions; the API path exists regardless of whether the configured component implements it.
- **A compare-and-set loop silently becomes last-writer-wins.** A store that returns no `ETag` on read gives the caller nothing to condition the write on, so the conditional path is never exercised and concurrent updates overwrite one another.
- **Duplicate side effects appear after a broker restart or a subscriber timeout.** Pub/sub delivery is at-least-once, so redelivery is normal behaviour rather than a fault, and non-idempotent handlers double-apply.
- **Latency regressions appear after adopting the sidecar for a chatty in-process path.** Every building-block call becomes a `localhost` round trip; a call previously served from a library in the same process now crosses a process boundary.
- **A component swap changes behaviour despite an unchanged application.** The API surface is stable, but per-component semantics — transaction support, ETag support, ordering — are not uniform, so correctness properties must be re-verified against the new backend.
