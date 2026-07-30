---
title: "Schema Evolution: what BACKWARD and FORWARD compatibility actually let you change"
date: 2026-07-30
track: microservices
summary: "A Schema Registry blocks incompatible changes at publish time — but only if you know which compatibility mode does what. Backward vs forward decides both which schema edits are legal and the order you must deploy producers and consumers. Here's the rulebook, with Avro examples and a curl that rejects a bad change."
reading_time: 6
tags: [schema-registry, avro, kafka, schema-evolution, contracts, compatibility]
sources:
  - title: "Schema Evolution and Compatibility — Confluent Documentation"
    url: "https://docs.confluent.io/platform/current/schema-registry/fundamentals/schema-evolution.html"
  - title: "Schema Resolution — Apache Avro Specification"
    url: "https://avro.apache.org/docs/1.12.0/specification/#schema-resolution"
  - title: "Building Microservices, 2nd ed. (Ch. 5, schemas & contracts) — Sam Newman"
    url: "https://samnewman.io/books/building_microservices_2nd_edition/"
  - title: "Schema Evolution with Apache Avro — INNOQ"
    url: "https://www.innoq.com/en/blog/2023/11/schema-evolution-avro/"
  - title: "Schema Registry API Reference — Confluent Documentation"
    url: "https://docs.confluent.io/platform/current/schema-registry/develop/api.html"
---

The moment two services exchange messages through a broker, they share a contract — the message schema — but they deploy on different schedules. Producer v2 ships Tuesday; some consumers won't upgrade until next sprint. A **Schema Registry** exists to make that safe: it stores each subject's schema history and, before accepting a new version, checks it against a **compatibility mode** you configured. Reject early, at publish, instead of discovering the break as a poison message in production (see the dead-letter-queue article here for what happens when you don't).

The whole thing hinges on two words that are easy to mix up: *backward* and *forward*. Get them right and evolution is mechanical.

## The two directions

Compatibility is defined by *who can read what*:

- **BACKWARD** (the Confluent default): a consumer using the **new** schema can read data written with the **previous** schema. You're protecting readers as they upgrade. Legal changes: **delete a field**, and **add an optional field** (one with a default). Deployment order: **upgrade consumers first**, then producers.
- **FORWARD**: a consumer using the **old** schema can read data written with the **new** schema. You're protecting readers who *haven't* upgraded yet. Legal changes: **add a field**, and **delete an optional field**. Deployment order: **upgrade producers first**, then consumers.
- **FULL**: both hold at once — you may only add or remove **optional** fields (fields with defaults). Safest, most restrictive.
- **NONE**: no checks. Don't.

Each has a `_TRANSITIVE` variant (`BACKWARD_TRANSITIVE`, etc.). Plain `BACKWARD` checks the new schema against the *immediately previous* version only; `BACKWARD_TRANSITIVE` checks it against *all* previous versions. Transitive is what you want if consumers might be reading a backlog written by any historical schema.

The mnemonic that sticks: **backward = new code reads old data; forward = old code reads new data.** Everything else follows.

## Why "optional" means "has a default"

Avro's schema resolution is the machinery underneath. When a reader with schema R decodes data written with schema W, it matches fields by name:

- A field in **W but not R** → the reader ignores it. (So a *reader* dropping a field is safe.)
- A field in **R but not W** → the reader needs a **default** to fill it in; without one, decoding fails.

That single rule explains the whole table. Adding a field is safe for old readers *only if the field has a default*, because old readers written against the pre-field schema still decode fine, and new readers decode old data by falling back to the default. A field without a default is a hard, breaking requirement on both ends.

```json
// v1
{"type": "record", "name": "Order", "fields": [
  {"name": "id",     "type": "string"},
  {"name": "amount", "type": "double"}
]}

// v2 — BACKWARD-compatible: new optional field with a default
{"type": "record", "name": "Order", "fields": [
  {"name": "id",       "type": "string"},
  {"name": "amount",   "type": "double"},
  {"name": "currency", "type": "string", "default": "USD"}
]}
```

A consumer on v2 reading a v1 record gets `currency = "USD"`. A consumer still on v1 reading a v2 record ignores the extra field. This particular change is actually **FULL**-compatible — add-optional-with-default is the safe intersection.

The breaking version is instructive:

```json
// v2-bad — no default. Old readers can't fill it in; new-schema reads of
// old data fail. Registry rejects this under BACKWARD or FULL.
{"name": "currency", "type": "string"}
```

## Enforce it, don't trust it

Set the mode on the subject and let the registry police pushes. First the mode:

```bash
curl -X PUT http://localhost:8081/config/orders-value \
  -H "Content-Type: application/vnd.schemaregistry.v1+json" \
  -d '{"compatibility": "BACKWARD"}'
```

Now test a candidate schema *before* your producer ships it — this is the check you wire into CI:

```bash
curl -X POST http://localhost:8081/compatibility/subjects/orders-value/versions/latest \
  -H "Content-Type: application/vnd.schemaregistry.v1+json" \
  -d '{"schema": "{\"type\":\"record\",\"name\":\"Order\",\"fields\":[{\"name\":\"id\",\"type\":\"string\"}]}"}'
# -> {"is_compatible": false}   # dropping a required field 'amount' with no default breaks BACKWARD
```

A `false` here is a build that never merges. That's the entire value proposition: the incompatible change dies in a pull request, not in a consumer's exception handler at 2 a.m.

## The order-of-deployment trap

Even a *compatible* change breaks if you deploy in the wrong order. Under BACKWARD, if you ship the producer first, old consumers may hit new data they weren't upgraded to handle. The rule pairs with the mode: **BACKWARD → consumers first; FORWARD → producers first.** Newman's advice in *Building Microservices* is to prefer **tolerant readers** (ignore unknown fields, don't over-validate) so a single service's rollout order stops being a distributed-systems problem. Compatibility modes give you the guardrail; tolerant readers give you the slack.

**Try next:** Stand up Schema Registry locally (the Confluent or Redpanda image), register the v1 `Order` schema under subject `orders-value`, set `BACKWARD`, then POST both the v2 (default) and v2-bad (no default) schemas to the `/compatibility` endpoint and confirm one returns `true` and the other `false`. Then flip the subject to `FORWARD` and watch which of the two changes flips its verdict — that contrast is the whole model in one experiment.
