---
title: "AsyncAPI 3.0: contract-first for the events between your services"
date: 2026-07-31
track: microservices
summary: "REST has OpenAPI; your Kafka and MQTT topics have had nothing comparable. AsyncAPI 3.0 (Dec 2023) finally split channels from operations and dropped the confusing publish/subscribe wording — here's a real document and what to do with it in CI."
reading_time: 5
tags: [asyncapi, event-driven, contracts, kafka, schemas, newman]
sources:
  - title: "AsyncAPI 3.0.0 Release Notes (Dec 5, 2023)"
    url: "https://www.asyncapi.com/blog/release-notes-3.0.0"
  - title: "AsyncAPI Specification v3.0.0 reference"
    url: "https://www.asyncapi.com/docs/reference/specification/v3.0.0"
  - title: "AsyncAPI Spec 3.1.0 Release Notes"
    url: "https://www.asyncapi.com/blog/release-notes-3.1.0"
---

When services talk over a broker instead of HTTP, the contract problem doesn't go away — it gets worse, because the coupling is now invisible. Nobody sees the shape of the `user.signedup` event until a consumer breaks. Newman's chapters on schemas and contract tests all assume you *have* a written schema to test against. For synchronous APIs that's OpenAPI. For event-driven ones it's **AsyncAPI**, and version 3.0 (released 5 December 2023, with 3.1 following as a compatible patch) is the first version that models messaging cleanly.

## What 3.0 fixed

The big change is that **channels and operations are separate top-level objects.** In 2.x a channel *was* an operation — a topic was welded to "this app publishes here" — so the same topic couldn't be described from both the producer's and the consumer's side without duplication. In 3.0:

- **`channels`** describe *where* messages flow (an address like `user/signedup`) and *what* messages can appear there. They're now reusable and referenceable across documents.
- **`operations`** describe *what an application does* with a channel, using the actions **`send`** and **`receive`** instead of the old `publish`/`subscribe` — which nobody could ever remember the direction of.
- **`messages`** is now a plural map, so a channel can carry several message types and each is individually `$ref`-able (this is what makes request/reply expressible).

Here is a minimal but complete 3.0 document for a signup event over MQTT:

```yaml
asyncapi: 3.0.0
info:
  title: User Service
  version: 1.0.0
servers:
  broker:
    host: mqtt.internal:1883
    protocol: mqtt
channels:
  userSignedup:
    address: user/signedup
    messages:
      UserSignedup:
        payload:
          type: object
          required: [id, email, ts]
          properties:
            id:    { type: string, format: uuid }
            email: { type: string, format: email }
            ts:    { type: integer, description: epoch millis }
operations:
  onUserSignedup:
    action: receive          # this app CONSUMES the event
    channel:
      $ref: '#/channels/userSignedup'
```

The producing service ships the *same* channel with an operation whose `action: send`. One channel definition, two viewpoints — that's the reuse 3.0 unlocked.

## Making it earn its keep in CI

A spec file that only humans read rots. Wire it into the pipeline so it fails builds:

```bash
# validate the document itself
npx @asyncapi/cli validate asyncapi.yaml

# generate typed models your producer/consumer import,
# so a payload change that breaks the schema breaks compilation
npx @asyncapi/modelina-cli generate typescript asyncapi.yaml -o ./generated
```

Add the validate step to every PR and the generated models to your build, and a breaking change to the `UserSignedup` payload can't merge silently — it either fails validation or fails to compile against the consumer. That's the same discipline consumer-driven contract tests give you for REST, applied to the broker.

A caveat worth stating: AsyncAPI describes the *envelope and routing* — topics, protocols, message shapes — but the payload schema is still just JSON Schema (or Avro/Protobuf via bindings). It documents that a Kafka topic exists and what rides on it; it does not run your broker's schema registry for you. Use it alongside the registry, not instead of it.

**Try next:** pick one event your services already exchange, write its channel + a `send` and a `receive` operation in a 3.0 doc, and add `asyncapi validate` to that repo's CI. Then change a field name in the payload and watch the pipeline catch it before a consumer does at 2 a.m.
