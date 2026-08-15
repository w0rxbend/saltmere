---
title: "AsyncAPI 3.0: contract-first for the events between services"
date: 2026-07-31
track: microservices
summary: "Representational State Transfer (REST) interfaces have OpenAPI; broker topics had no equivalent. AsyncAPI 3.0 (December 2023) separates channels from operations and replaces publish/subscribe with send/receive — a worked document and its role in continuous integration."
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

**Gist.** When services communicate through a message broker rather than over Hypertext Transfer Protocol (HTTP) request/response, the coupling between them stops being visible at any call site: no code references the consumer, so the shape of an event such as `user.signedup` is discovered only when a consumer fails to parse it. AsyncAPI is a machine-readable description format for that coupling, and **version 3.0.0, released 5 December 2023, models a channel and an application's use of that channel as two separate objects**, so one topic definition serves both producer and consumer. The cost is a second artefact to keep synchronised with the running code, and a description that covers routing and message shape without enforcing anything at the broker, which leaves runtime rejection of a bad payload to a schema registry if one is deployed.

## The 2.x structure and what 3.0 changed

In AsyncAPI 2.x a channel item carried the operation directly: the channel entry held a `publish` or a `subscribe` block. **A channel therefore encoded both an address and a direction**, and a topic described from the producing side could not be reused verbatim by the consuming side — the second document had to restate the address, the message, and the bindings under the opposite keyword. Duplication of that kind drifts: the two copies of the payload schema diverge, and the divergence is discovered at runtime.

Version 3.0.0 splits the document along three axes.

- **`channels` describe where messages flow.** A channel entry holds an `address` (for example `user/signedup`), the servers it applies to, and the messages that may appear on it. It states nothing about direction, so a single definition is valid from either side and can be referenced from other documents.
- **`operations` describe what one application does with a channel.** An operation names its `channel` by reference and carries an `action` whose value is **`send`** or **`receive`**, replacing 2.x's `publish` and `subscribe`.
- **`messages` is a map rather than a single value.** A channel can therefore carry several message types, and each entry is individually addressable by JSON Reference (`$ref`). Request/reply is modelled separately, by a `reply` object on the operation rather than by the channel.

The direction rename is the part that changes review outcomes rather than only structure. Under `publish`/`subscribe` the keyword described what a *client of the document* would do, which inverted relative to the application the document described. **`send` and `receive` are stated from the perspective of the application the document belongs to**: `receive` means this application consumes the message.

A minimal 3.0.0 document for a signup event carried over Message Queuing Telemetry Transport (MQTT):

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
    action: receive          # this application consumes the event
    channel:
      $ref: '#/channels/userSignedup'
```

The producing service publishes the same channel object with an operation whose `action` is `send`. **One channel definition, two operation objects, no duplicated payload schema** — the reuse that the 2.x structure prevented.

Version 3.1.0 followed 3.0.0; its release notes record the additions made on top of the 3.0 structure.

## Enforcement in continuous integration

A description that only humans read diverges from the code it describes, because nothing fails when it does. Two commands turn it into a build gate:

```bash
# validate the document against the AsyncAPI schema
npx @asyncapi/cli validate asyncapi.yaml

# generate typed models the producer and consumer import,
# so a payload change that breaks the schema breaks compilation
npx @asyncapi/modelina-cli generate typescript asyncapi.yaml -o ./generated
```

The first command checks the document itself: an unresolvable `$ref`, an `action` outside the `send`/`receive` set, or a malformed channel fails the step. Nothing in that check reaches the code. **The coupling to the code comes from the second command**: when the generated models are compiled as part of the build, a field renamed in the payload schema and not renamed in the consumer produces a compilation failure in the consumer's repository, and a field renamed in the consumer without a schema change produces the same failure against the regenerated model. That is the property consumer-driven contract testing supplies for synchronous interfaces, obtained here through the type system rather than through a recorded interaction.

The boundary of the format matters for what this gate can catch. **AsyncAPI describes the envelope and the routing** — addresses, protocols, servers, bindings, message shapes. The payload schema is JSON Schema by default, or Avro or Protocol Buffers referenced through a schema-format binding. A document therefore records that a topic exists and what is meant to travel on it; it does not enforce anything at broker level, and it does not replace a schema registry that rejects incompatible writes at produce time. The two are complementary: the registry enforces at runtime, the document plus generated models enforce at build time.

## Pitfalls

- **Reading `send`/`receive` from the reader's perspective inverts the wiring.** An operation with `action: receive` in a producer's document declares that the producer consumes the message; a service generated from a misread action subscribes to a topic it was meant to publish to, and the topic stays silent with no error anywhere.
- **Validating the document without compiling the generated models catches nothing about the code.** `asyncapi validate` accepts any well-formed schema, including one that no longer matches the class the producer serialises, so the pipeline stays green while the payload drifts.
- **Carrying 2.x habits into a 3.0 file duplicates the payload.** Writing one channel per direction reintroduces the two copies of the message schema that the channel/operation split exists to avoid, and the copies diverge on the next field addition.
- **Treating the document as a substitute for a schema registry leaves produce-time writes unchecked.** Nothing in the specification runs at the broker; a producer that bypasses the generated model can still write a payload the document forbids.
- **A `$ref` to a message inside a channel that is later renamed breaks silently in editors that resolve references lazily.** The failure appears only when the document is validated or a generator resolves the reference, which may be a separate pipeline from the one that edited the file.
