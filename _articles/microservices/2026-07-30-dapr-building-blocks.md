---
title: "Dapr Building Blocks: Swap Redis for Kafka Without Touching Your Code"
date: 2026-07-30
track: microservices
summary: "Dapr runs as a sidecar and exposes pluggable building blocks — state, pub/sub, service invocation, actors, workflow — behind a stable HTTP/gRPC API. You wire the backing infra in component YAML, so switching Redis to Postgres or Kafka is a config change, not a rewrite."
reading_time: 6
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

Most microservice glue code is the same problem solved fifteen times: retry a call, discover a service, persist a key, publish an event, fetch a secret. Every language does it differently, every SDK version drifts, and swapping Redis for Postgres means editing code in six services. Dapr's bet is that this glue is infrastructure, not business logic — so it should live in a sidecar behind a stable API.

Dapr (Distributed Application Runtime) is a CNCF **graduated** project as of **November 12, 2024** (it entered incubation in November 2021). The current stable runtime is **v1.18.2**, released July 21, 2026. This piece covers what the building blocks are and shows the state and pub/sub APIs concretely.

## The sidecar model

Dapr runs as a separate process next to your app — a sidecar container in Kubernetes, or a local process under `dapr run`. Your code never imports a Redis client or a Kafka client. Instead it talks to the sidecar over `localhost` via HTTP or gRPC, and the sidecar talks to the real infrastructure.

The result: your service knows there is "a state store" and "a pub/sub broker." Which store, which broker, and how to authenticate to it are declared in **component YAML** that you deploy alongside the app. Change the YAML, restart the sidecar, and the same code now writes to a different backend.

## The building blocks

A building block is a capability exposed at a stable API path (`/v1.0/state`, `/v1.0/publish`, `/v1.0/invoke`, …) and backed by a pluggable **component**. As of v1.18 the catalog is:

| Building block | What it does |
|---|---|
| Service invocation | Call another app by ID with discovery, mTLS, retries, tracing |
| State management | Key/value persistence over pluggable stores |
| Publish & subscribe | At-least-once topic messaging, sender/receiver decoupled |
| Bindings | Bi-directional connectors to external systems (input triggers + output) |
| Actors | Virtual actors: single-threaded units of state + compute |
| Secrets | Read secrets from vaults, cloud stores, or Kubernetes |
| Configuration | Read and subscribe to config items |
| Distributed lock | Mutual exclusion across app instances |
| Cryptography | Encrypt/decrypt without exposing keys to the app |
| Jobs | Schedule delayed and recurring work |
| Workflow | Durable, crash-resilient orchestration of long-running processes |
| Conversation | A unified API to prompt LLMs, with retries and PII scrubbing |

The point is not to memorize the list — it is that each one hides an interchangeable backend behind an identical call, and that the API contract stays put across implementations.

## State management, concretely

Declare a state store as a component. Here is a Redis store for local development:

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

Note `metadata.name: statestore` — that string is the store name your API calls reference, *not* `redis`. Now save, read, and delete a key by talking only to the sidecar (assume it is on port 3500):

```bash
# Save — body is a JSON array of {key, value} objects
curl -X POST http://localhost:3500/v1.0/state/statestore \
  -H "Content-Type: application/json" \
  -d '[{ "key": "order-42", "value": { "status": "packed" } }]'

# Read it back
curl http://localhost:3500/v1.0/state/statestore/order-42
# -> {"status":"packed"}

# Delete
curl -X DELETE http://localhost:3500/v1.0/state/statestore/order-42
```

The paths are the whole story: `POST /v1.0/state/<store>` to write, `GET /v1.0/state/<store>/<key>` to read, `DELETE /v1.0/state/<store>/<key>` to remove. To move from Redis to PostgreSQL you change `spec.type` to `state.postgresql` and its metadata — the three curl commands above are untouched.

Dapr also layers optional features on the same paths: pass an `etag` in the write body for optimistic concurrency (returned as an `ETag` header on read, enforced with `If-Match` on delete), and `POST /v1.0/state/<store>/transaction` for multi-key atomic writes when the backend supports it.

## Pub/sub, concretely

A pub/sub component looks the same shape:

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

Publishing is one call — `POST /v1.0/publish/<pubsub-name>/<topic>`:

```bash
curl -X POST http://localhost:3500/v1.0/publish/orderpubsub/orders \
  -H "Content-Type: application/json" \
  -d '{ "orderId": 42, "total": 19.99 }'
```

Dapr wraps the payload in a CloudEvents envelope, delivers it at least once, and handles retries. Subscribers register a route (declaratively or via a `/dapr/subscribe` endpoint), and Dapr POSTs each message to that route. Swap `pubsub.redis` for `pubsub.kafka` — or Azure Service Bus, or RabbitMQ — and the publish call and subscriber routes do not change. Because delivery is at-least-once, subscribers still need to be idempotent; the runtime does not make duplicates disappear.

## Service invocation

The other call you will use constantly is service-to-service. Instead of resolving hostnames and wiring mTLS yourself:

```bash
curl http://localhost:3500/v1.0/invoke/checkout/method/health
```

`checkout` is the target app's Dapr app-ID; `health` is the method (a path on that app). Dapr handles discovery, mutual TLS between sidecars, retries, and distributed-trace propagation. Your app just hits `localhost`.

## Where it fits — and where it doesn't

Dapr shines when you have polyglot services and want to standardize the boring cross-cutting concerns, or when you expect the backing infrastructure to change (dev on Redis, prod on a managed cloud store) without code churn. The stable API contract is the real product; components are the swappable part.

It is not free. You run an extra process per app, which adds a network hop and a small latency tax to every call, plus the operational surface of the Dapr control plane on Kubernetes. For a single-language monolith-ish service talking to one database it owns outright, a plain client library is simpler. Dapr pays off as the number of services, languages, and backends grows.

**Try next:** `dapr init`, drop the `statestore.yaml` above into a `components/` folder, run `dapr run --app-id demo --dapr-http-port 3500 --resources-path ./components -- sleep 3600`, then fire the three state curls at `localhost:3500` and watch the key round-trip through Redis.
