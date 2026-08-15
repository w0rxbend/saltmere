---
title: "Buf and Protobuf Breaking-Change Detection: A Schema Registry for gRPC Contracts"
date: 2026-08-15
track: microservices
summary: "A gRPC contract is only as safe as the discipline stopping a rename or a reused tag number. The buf CLI replaces that discipline with a check: buf breaking compares a .proto module against a git ref or the Buf Schema Registry and fails the build on wire-incompatible edits. Covers the v2 configuration toolchain, from buf.yaml to a CI gate."
reading_time: 6
tags: [buf, protobuf, grpc, breaking-changes, schema-registry, bsr]
sources:
  - title: "Detecting breaking changes — Buf Docs"
    url: "https://buf.build/docs/breaking/"
  - title: "buf.yaml configuration (v2) — Buf Docs"
    url: "https://buf.build/docs/configuration/v2/buf-yaml/"
  - title: "The Buf Schema Registry (BSR) — Buf Docs"
    url: "https://buf.build/docs/bsr/"
  - title: "Avoiding Common Protobuf Pitfalls with Buf — Earthly Blog"
    url: "https://earthly.dev/blog/buf-protobuf/"
  - title: "A CI pipeline for Protobuf with GitLab CI and Buf — Maciej Mionskowski"
    url: "https://mionskowski.pl/posts/ci-pipeline-for-protobuf/"
---

**Gist.** A Protocol Buffers (Protobuf) `.proto` file is a contract, but `protoc` enforces nothing about its evolution: changing a field's type, deleting it, or reusing a retired tag number still compiles, and the incompatibility surfaces only when a peer built against the old schema decodes bytes written against the new one. The `buf` command-line interface (CLI) makes that latent break a failed check by comparing the current schema against a previous version — a git ref or a module published to the **Buf Schema Registry (BSR)** — and reporting each edit that violates a chosen compatibility category. The cost is a declared compatibility contract that must be picked deliberately, plus a dependency on a registry (or a private instance) once cross-repository checking is wanted.

## The failure the compiler cannot see

Protobuf's wire format carries **field numbers and wire types, not names**. A varint field tagged `3` is encoded as a key byte derived from `3` and the wire type, followed by the value. A decoder built from an older schema matches on that tag alone. Three edits are therefore invisible to every compiler and lethal at runtime:

- **Reusing a tag number** that a deleted field once held. Old writers still emit the old semantics under tag `3`; new readers interpret those bytes as the new field. Nothing errors — the message decodes into a wrong value.
- **Changing a field's declared type** where the wire type happens to survive. `int32` and `int64` share the varint wire type, so a value written by one side is accepted by the other and reinterpreted.
- **Renumbering an enum.** The numeric value travels; the symbolic name is a local artefact of generated code.

The failure mode is **silent corruption, not a decode error**, which is what makes a static check worth its configuration cost.

## Two configuration files

Buf splits configuration by concern. **`buf.yaml`** defines the module: where the Protobuf lives, which lint and breaking rules apply, and which BSR dependencies it pulls in. The current schema version is **v2**.

```yaml
version: v2
modules:
  - path: proto
    name: buf.build/acme/paymentapis
lint:
  use:
    - STANDARD          # default rule set; passing STANDARD implies BASIC and MINIMAL
  except:
    - PACKAGE_VERSION_SUFFIX
breaking:
  use:
    - WIRE_JSON         # forbid changes that break binary OR JSON encoding
deps:
  - buf.build/googleapis/googleapis
```

**`buf.gen.yaml`** is separate and describes code generation — which plugins run and where output lands. Plugins may be declared as **remote plugins** hosted on the BSR, in which case no contributor needs `protoc` or a plugin binary installed locally.

```yaml
version: v2
managed:
  enabled: true
plugins:
  - remote: buf.build/protocolbuffers/go:v1.36.0
    out: gen/go
    opt: paths=source_relative
  - remote: buf.build/grpc/go:v1.5.1
    out: gen/go
    opt: paths=source_relative
inputs:
  - directory: proto
```

`buf generate` reads that file and writes stubs into `gen/go`. **The toolchain version is pinned in the repository, not in an individual shell**, which removes the class of diff noise produced by contributors running different plugin builds.

## Linting

`buf lint` enforces an evolution-friendly style — package versioning, field naming, request/response conventions — from five built-in categories: **MINIMAL**, **BASIC**, **STANDARD** (the default), **COMMENTS**, and **UNARY_RPC**. The categories nest: anything passing STANDARD also passes BASIC and MINIMAL.

```console
$ buf lint
proto/payment/v1/payment.proto:14:3: Field name "chargeID" should be lower_snake_case, such as "charge_id". (FIELD_LOWER_SNAKE_CASE)
```

## Breaking-change detection and its four categories

`buf breaking` compares the current schema against a **past** version named by `--against`, which accepts any input the CLI understands — most usefully a git ref or a BSR module.

```console
# compare the working tree against the tip of main
$ buf breaking --against '.git#branch=main'

# compare against the published contract in the registry
$ buf breaking --against 'buf.build/acme/paymentapis'
```

Each violation names the rule and the symbol it fired on:

```console
$ buf breaking --against '.git#branch=main'
proto/payment/v1/payment.proto:1:1: Previously present field "3" with name "amount_cents" on message "Charge" was deleted.
proto/payment/v1/payment.proto:22:3: Field "5" on message "Charge" changed type from "int32" to "int64".
```

The rules are grouped into four categories of decreasing strictness. **FILE** is the default and catches anything that changes generated per-file source. **PACKAGE** operates at package granularity. **WIRE_JSON** forbids changes that break binary *or* JavaScript Object Notation (JSON) encoding. **WIRE** covers the binary wire format alone.

**Selecting a category is selecting the compatibility contract, and it decides which edits are legal.** `WIRE` permits a field rename, because the name does not appear on the binary wire. `FILE` forbids the same rename, because it changes the generated symbol that consumer code imports. The choice follows from how consumers integrate: `FILE` where they regenerate from the `.proto` and compile against the symbols; `WIRE` or `WIRE_JSON` where they only exchange encoded bytes.

## Registry-backed checking and the CI gate

`--against '.git#branch=main'` bounds the check to one repository. It does not cover the cross-team case: one team ships a schema change and a consuming team in a different repository, on a different deploy cadence, is the one that breaks. `buf push` publishes a module to the BSR, where **each push is a commit and labels mark commits for consumers**.

```console
$ buf push
buf.build/acme/paymentapis:c3f9a1...   # commit id, with the label main moved to it
```

Publication enables three things. Downstream teams **depend on the module by name** in their own `buf.yaml` `deps` rather than vendoring a `.proto` snapshot. The BSR **generates client libraries** for Go, npm, Python and others, so consumers install SDKs through their normal package manager and run no `protoc` at all. And any repository can run `buf breaking --against buf.build/acme/paymentapis`, making **the published contract the single artefact every team validates against** rather than a branch tip that varies per checkout.

A continuous integration (CI) step turns the check into a merge gate:

{% raw %}
```yaml
- uses: bufbuild/buf-action@v1
  with:
    lint: true
    breaking: true
    breaking_against: 'https://github.com/${{ github.repository }}.git#branch=main'
```
{% endraw %}

A pull request that deletes a field or reuses a tag number then fails before merge — the same guardrail shape as a schema-registry compatibility check or a consumer-driven contract test, applied to the `.proto` itself.

The limits are worth stating plainly. `buf breaking` reasons about the **schema only**: it catches a type change or a deleted field, but it cannot know that a service has begun treating a structurally compatible field as required, so semantic compatibility remains unchecked. Adopting the BSR adds a dependency on Buf's hosted registry or the operational cost of a private instance. And the check is exactly as strict as the configured category.

## Pitfalls

- **`breaking: use: WIRE` lets field renames merge.** Consumers that regenerate code then fail to compile against the new symbol names, with no CI signal on the producing repository.
- **`--against` pointed at the working branch reports nothing.** Comparing a branch against itself yields an empty diff, so the gate passes vacuously; the target must be the shared base, such as `.git#branch=main`.
- **Reusing a retired tag number produces no decode error.** Old writers and new readers agree on the tag and disagree on the meaning, so the message decodes into a wrong value rather than failing.
- **`int32` to `int64` survives a binary-only check.** Both use the varint wire type, so only a category that also inspects the declared type reports the change.
- **A `.proto` never pushed to the BSR cannot be validated cross-repository.** Consumers left validating against a vendored snapshot check against a copy that drifts from the producer's contract.
- **Plugin versions unpinned in `buf.gen.yaml` yield generated diffs unrelated to schema edits.** Two contributors on different plugin builds produce different `gen/` output from identical input.
