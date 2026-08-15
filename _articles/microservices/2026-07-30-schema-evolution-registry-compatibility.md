---
title: "Schema Evolution: what BACKWARD and FORWARD compatibility permit"
date: 2026-07-30
track: microservices
summary: "A Schema Registry rejects incompatible changes at publish time, but the compatibility mode decides which schema edits are legal and in which order producers and consumers must be deployed. This article states the rulebook, derives it from Avro schema resolution, and shows the registry call that rejects a bad change."
reading_time: 7
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

**Gist.** Two services that exchange messages through a broker share a contract — the message schema — but deploy on independent schedules, so at any moment data written under one schema version is being read under another. A **Schema Registry** stores each subject's schema history and validates every candidate version against a configured **compatibility mode** before accepting it, moving the failure from a poison message in production to a rejected publish. The cost is that the mode constrains which edits are legal at all, and it also fixes the order in which producers and consumers must be rolled out.

## The two directions

Compatibility is defined in terms of which reader can decode which writer's data.

- **BACKWARD** (the Confluent default): a consumer using the **new** schema can read data written with the **previous** schema. The protected party is the reader as it upgrades. Legal changes: **delete a field**, and **add an optional field** (one carrying a default). Deployment order: **consumers first**, then producers.
- **FORWARD**: a consumer using the **old** schema can read data written with the **new** schema. The protected party is the reader that has not upgraded. Legal changes: **add a field**, and **delete an optional field**. Deployment order: **producers first**, then consumers.
- **FULL**: both properties hold simultaneously, so only **optional** fields (fields with defaults) may be added or removed. It is the most restrictive of the three.
- **NONE**: no check is performed.

Each of the three checking modes has a `_TRANSITIVE` variant (`BACKWARD_TRANSITIVE`, `FORWARD_TRANSITIVE`, `FULL_TRANSITIVE`); `NONE` has none. Plain `BACKWARD` checks the candidate against the **immediately previous version only**; `BACKWARD_TRANSITIVE` checks it against **all previous versions** of the subject. The distinction is load-bearing whenever consumers replay a retained backlog, because such a backlog may contain records written under any historical schema, not merely the latest one. A chain of individually-compatible steps does not compose: v1→v2 and v2→v3 can each pass plain `BACKWARD` while v3 cannot read v1 data.

The invariant to carry: **backward means new code reads old data; forward means old code reads new data.** The legal-change tables follow from that sentence rather than needing to be memorised.

## Why "optional" means "carries a default"

Avro's schema resolution is the machinery underneath, and the compatibility table is a consequence of it. When a reader holding schema R decodes a record written with schema W, record fields are matched **by name**, not by position:

- A field present in **W but absent from R** — the reader ignores the encoded value. Removing a field from the *reader's* schema is therefore safe.
- A field present in **R but absent from W** — the reader must substitute the field's **default**. If the field declares no default, resolution fails and decoding raises an error.

That single asymmetry explains every row of the table. Adding a field is safe for readers on the older schema because they ignore what they do not recognise; it is safe for readers on the newer schema reading older data **only if the field declares a default**, since that is the only value available to fill the gap. A field without a default is a hard requirement imposed on both ends at once.

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

A consumer on v2 reading a v1 record observes `currency = "USD"`. A consumer still on v1 reading a v2 record discards the extra field. This change satisfies **FULL**: add-optional-with-default is the intersection of the two directions.

The rejected variant is the instructive one:

```json
// v2-bad — no default. Readers on v2 cannot fill the field when decoding v1
// data. The registry rejects this under BACKWARD and under FULL.
{"name": "currency", "type": "string"}
```

### Implementation sketch (Scala)

The resolution rule above is small enough to state directly. The following models record schemas as ordered field lists and decides whether a reader schema can decode a writer's data — the predicate a `BACKWARD` check applies with R as the new schema and W as the previous version, and a `FORWARD` check applies with the arguments exchanged.

```scala
final case class Field(name: String, avroType: String, default: Option[String])
final case class RecordSchema(name: String, fields: List[Field])

enum Resolution:
  case Ok
  case Missing(field: String)     // in reader, absent from writer, no default
  case TypeMismatch(field: String, reader: String, writer: String)

def canRead(reader: RecordSchema, writer: RecordSchema): List[Resolution] =
  val written: Map[String, Field] = writer.fields.map(f => f.name -> f).toMap
  reader.fields.flatMap: r =>
    written.get(r.name) match
      case Some(w) if w.avroType == r.avroType => None
      case Some(w) => Some(Resolution.TypeMismatch(r.name, r.avroType, w.avroType))
      // absent from the writer: only a declared default makes resolution succeed
      case None    => r.default.fold(Some(Resolution.Missing(r.name)))(_ => None)
  // fields in the writer and not the reader need no handling: they are skipped

def isBackwardCompatible(candidate: RecordSchema, previous: RecordSchema): Boolean =
  canRead(reader = candidate, writer = previous).isEmpty

def isForwardCompatible(candidate: RecordSchema, previous: RecordSchema): Boolean =
  canRead(reader = previous, writer = candidate).isEmpty

def isFull(candidate: RecordSchema, previous: RecordSchema): Boolean =
  isBackwardCompatible(candidate, previous) && isForwardCompatible(candidate, previous)

// transitive variants quantify over the whole subject history, not just the tip
def isBackwardTransitive(candidate: RecordSchema, history: List[RecordSchema]): Boolean =
  history.forall(isBackwardCompatible(candidate, _))
```

The sketch omits aliases, union and enum resolution, and numeric type promotion, all of which the Avro specification defines; it retains the part that decides the compatibility table.

## Enforcement

The mode is configured per subject, and the registry then rejects non-conforming pushes:

```bash
curl -X PUT http://localhost:8081/config/orders-value \
  -H "Content-Type: application/vnd.schemaregistry.v1+json" \
  -d '{"compatibility": "BACKWARD"}'
```

A candidate schema can be tested before the producer ships, which is the check that belongs in continuous integration:

```bash
curl -X POST http://localhost:8081/compatibility/subjects/orders-value/versions/latest \
  -H "Content-Type: application/vnd.schemaregistry.v1+json" \
  -d '{"schema": "{\"type\":\"record\",\"name\":\"Order\",\"fields\":[{\"name\":\"id\",\"type\":\"string\"},{\"name\":\"amount\",\"type\":\"double\"},{\"name\":\"currency\",\"type\":\"string\"}]}"}'
# -> {"is_compatible": false}   # 'currency' has no default, so a reader on the
#    candidate schema cannot decode records written without it: BACKWARD fails
```

A `false` verdict is a build that does not merge: the incompatible change fails in review rather than in a consumer's exception handler.

## Order of deployment

A change that passes the compatibility check still breaks the system if the rollout order contradicts the mode. Under `BACKWARD`, shipping the producer first exposes consumers still on the old schema to data written under the new one — a direction `BACKWARD` never claimed to cover. The pairing is fixed: **BACKWARD implies consumers first; FORWARD implies producers first.**

Newman's treatment of schemas and contracts in *Building Microservices* describes the **tolerant reader**: a consumer that extracts only the fields it uses and ignores the rest of the payload rather than validating it whole. The compatibility mode supplies the guardrail; a tolerant reader narrows the set of schema changes that can break any one consumer, because a field it never reads cannot fail its parse.

An experiment that exercises the whole model: register the v1 `Order` schema under subject `orders-value` against a local registry, set `BACKWARD`, POST both v2 (with default) and v2-bad (without) to the `/compatibility` endpoint, and confirm the first returns `true` and the second `false`. Switching the subject to `FORWARD` makes both return `true`: adding a field is forward-compatible whether or not it declares a default, so the pair separates only under `BACKWARD` and `FULL`. Deleting `amount` is the change that separates them the other way — accepted under `BACKWARD`, rejected under `FORWARD`.

## Pitfalls

- **Plain `BACKWARD` passes a chain that breaks on replay.** Each version is checked only against its immediate predecessor, so a consumer on v3 reading a retained v1 record can fail resolution even though every individual registration was accepted. `BACKWARD_TRANSITIVE` is the mode that quantifies over the full history.
- **Adding a field without a default is rejected under BACKWARD and FULL, not merely discouraged.** A reader on the new schema has no value to substitute when the writer's record lacks the field, so resolution fails rather than yielding a null or zero.
- **Renaming a field is a delete plus an add.** Avro matches fields by name, so the renamed field is unmatched in the counterpart schema and is governed by the delete and add rules independently — under `FULL` a rename is legal only when both the old and the new field carry defaults.
- **Deploying producers first under `BACKWARD` breaks consumers even though the registry accepted the schema.** The check certifies that new readers can decode old data; it certifies nothing about old readers decoding new data.
- **`NONE` disables the check without disabling the consequence.** Registration succeeds and the failure surfaces at decode time in the consumer.
- **Compatibility is per subject.** A schema reused across topics is validated against the history of the subject it is registered under, so the same change can be accepted for one subject and rejected for another whose history differs.
