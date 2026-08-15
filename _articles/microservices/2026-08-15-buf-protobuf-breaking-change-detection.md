---
title: "Buf and Protobuf Breaking-Change Detection: A Schema Registry for Your gRPC Contracts"
date: 2026-08-15
track: microservices
summary: "A gRPC contract is only as safe as the discipline stopping someone from renaming a field or reusing a tag number. Buf replaces that discipline with a check: buf breaking compares your .proto against a git ref or the Buf Schema Registry and fails the build on wire-incompatible edits. This is the current toolchain — buf CLI 1.72.0 (July 2026) — from buf.yaml to a CI gate."
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

A `.proto` file is a contract, but nothing in `protoc` enforces that you keep it. Change field `3` from `int32` to `int64`, reuse a tag number a deleted field once held, or renumber an enum — the code still generates, the build still passes, and the break only surfaces when a service compiled against the old schema decodes bytes written against the new one. On the wire, that's a silently corrupted message, not an error.

**Buf** turns that latent break into a failed check. The `buf` CLI (**1.72.0**, released July 17 2026) lints Protobuf, detects breaking changes against a previous version, and — through the **Buf Schema Registry (BSR)** — gives your `.proto` files the same versioned, dependency-managed home your code already has. The whole thing is driven by two small YAML files and a couple of commands.

## Two config files: buf.yaml and buf.gen.yaml

Buf splits configuration by concern. **`buf.yaml`** defines the module — where your Protobuf lives, what lint and breaking rules apply, and which BSR dependencies it pulls in. The current schema is **v2**:

```yaml
version: v2
modules:
  - path: proto
    name: buf.build/acme/paymentapis
lint:
  use:
    - STANDARD          # the default rule set; also STANDARD implies BASIC and MINIMAL
  except:
    - PACKAGE_VERSION_SUFFIX
breaking:
  use:
    - WIRE_JSON         # forbid changes that break binary OR JSON encoding
deps:
  - buf.build/googleapis/googleapis
```

**`buf.gen.yaml`** is separate and describes code generation — which plugins run and where output lands. In 2026 the idiomatic form uses **remote plugins** hosted on the BSR, so no contributor needs `protoc` or a plugin binary installed locally:

```yaml
version: v2
managed:
  enabled: true
plugins:
  - remote: buf.build/protocolbuffers/go:v1.36.11
    out: gen/go
    opt: paths=source_relative
  - remote: buf.build/grpc/go
    out: gen/go
    opt: paths=source_relative
inputs:
  - directory: proto
```

`buf generate` then reads that file and writes stubs into `gen/go` — the toolchain is pinned in the config, not in each engineer's shell.

## buf lint and buf breaking

Linting comes first. `buf lint` enforces a consistent, evolution-friendly style — package versioning, field naming, RPC request/response conventions — from five built-in categories: **MINIMAL**, **BASIC**, **STANDARD** (the default), **COMMENTS**, and **UNARY_RPC**. Anything passing STANDARD also passes BASIC and MINIMAL.

```console
$ buf lint
proto/payment/v1/payment.proto:14:3: Field name "chargeID" should be lower_snake_case, such as "charge_id". (STYLE)
```

The one that changes how teams work is `buf breaking`. It compares your current schema against a **past** version and reports edits that would break clients, servers, or generated code. The comparison target is the `--against` flag, and it accepts any input the CLI understands — most usefully a **git ref** or a **BSR module**:

```console
# compare working tree against the tip of main
$ buf breaking --against '.git#branch=main'

# compare against the published contract in the registry
$ buf breaking --against 'buf.build/acme/paymentapis'
```

A break prints exactly what rule fired and why:

```console
$ buf breaking --against '.git#branch=main'
proto/payment/v1/payment.proto:1:1: Previously present field "3" with name "amount_cents" on message "Charge" was deleted.
proto/payment/v1/payment.proto:22:3: Field "5" on message "Charge" changed type from "int32" to "int64".
```

The breaking rules come in four categories of increasing strictness: **FILE** (the default — catches anything that changes generated per-file source, the safest bar), **PACKAGE**, **WIRE_JSON** (binary *or* JSON encoding breaks), and **WIRE** (binary wire format only). Choosing a category is choosing your compatibility contract: `WIRE` permits field renames because they're invisible on the binary wire, while `FILE` forbids them because they break the generated symbol your Go or Java code imports. Pick `FILE` when consumers regenerate from your `.proto`; pick `WIRE`/`WIRE_JSON` when they only exchange bytes.

## The BSR: a registry for schemas, and the CI gate

The `--against '.git#branch=main'` form catches breaks *within* one repo. The problem it doesn't solve is cross-team: the orders team ships a schema change, and the payments team — a separate repo, a separate deploy cadence — is the one that breaks. That's what the **Buf Schema Registry** is for. `buf push` sends a module to the BSR, where every push is a commit and labels mark commits for consumers:

```console
$ buf push
buf.build/acme/paymentapis:c3f9a1... (labeled: main)
```

Once the contract is published, three things follow. Downstream teams **depend on it by name** in their own `buf.yaml` `deps` instead of vendoring a `.proto` snapshot. They **install generated SDKs** for Go, npm, Python, and more straight from their normal package manager — the BSR generates client libraries so no consumer runs `protoc` at all. And crucially, anyone can point `buf breaking --against buf.build/acme/paymentapis` at the *published* contract, making the registry the source of truth every team validates against.

Wire that into CI and the guarantee becomes mechanical. A minimal GitHub Actions step:

```yaml
- uses: bufbuild/buf-action@v1
  with:
    lint: true
    breaking: true
    breaking_against: 'https://github.com/${{ github.repository }}.git#branch=main'
```

Now a pull request that deletes a field or reuses a tag number fails before merge — the same shape of guardrail as a schema-registry compatibility check or a consumer-driven contract test, but enforced on the `.proto` itself.

The trade-offs are worth stating. `buf breaking` reasons purely about the *schema*: it will catch a type change or a deleted field, but it cannot know that your service treats a still-compatible field as newly required, so semantic compatibility is still on you. Adopting the BSR adds a dependency on Buf's registry (or the operational cost of running a private instance). And the checks are only as strict as the category you choose — set `breaking: use: WIRE` and field renames sail through. Configured deliberately, though, Buf converts "please remember not to break the contract" into a build that won't go green if you do.

**Try next:** In a repo with a `.proto`, add the `buf.yaml` above, commit, then rename a field or change its type and run `buf breaking --against '.git#branch=main'`. Watch it name the exact field and rule. Then flip `breaking: use:` from `FILE` to `WIRE`, rerun, and see which of your edits the looser category now lets through — that contrast is the whole compatibility model in one command.
