---
title: "OpenTelemetry Weaver: Semantic Conventions as Compilable Code"
date: 2026-07-31
track: observability
summary: "How OpenTelemetry Weaver resolves a semantic-convention registry into type-safe constants, checks it in CI, and validates live telemetry so attribute names like http.request.method stop drifting."
reading_time: 5
tags: [opentelemetry, semantic-conventions, weaver, observability, code-generation, policy-as-code]
sources:
  - title: "Observability by Design: Unlocking Consistency with OpenTelemetry Weaver (OTel blog)"
    url: "https://opentelemetry.io/blog/2025/otel-weaver/"
  - title: "open-telemetry/weaver (GitHub repo)"
    url: "https://github.com/open-telemetry/weaver"
  - title: "Weaver command-line usage reference"
    url: "https://github.com/open-telemetry/weaver/blob/main/docs/usage.md"
  - title: "Generating semantic convention libraries (OTel semconv spec)"
    url: "https://opentelemetry.io/docs/specs/semconv/non-normative/code-generation/"
  - title: "Making Semantic Conventions Work for You With OpenTelemetry Weaver (Honeycomb)"
    url: "https://www.honeycomb.io/blog/making-semantic-conventions-work-opentelemetry-weaver"
---

**Gist.** Telemetry attribute names are a contract enforced nowhere: a dashboard filters on `http.request.method`, but any service is free to emit `http.method` instead, and the divergence surfaces only as a silently empty panel. OpenTelemetry Weaver treats the semantic-convention registry as a compilable artifact — resolving it, gating it with policy, generating language constants from it, and comparing live OpenTelemetry Protocol (OTLP) traffic against it. The cost is a build-time dependency on the registry: the convention becomes a versioned source input that must be resolved, policed and regenerated whenever it changes.

## The drift problem

An attribute name is a join key. A dashboard filters on it, an alert groups by it, a trace query joins on it. Nothing in the emitting path checks it. Service A emits `http.method`, service B emits the misspelling `http.reqest.method`, and a third team writes `http_request_method` after borrowing a metric-style separator. **Each variant produces a distinct time series rather than an error**, so the failure is additive and silent: queries keep returning results, they return a shrinking fraction of the traffic. Detection typically waits for an incident in which an alert does not fire.

OpenTelemetry's semantic conventions define the correct names across many domains, but a specification rendered as a Markdown table imposes nothing at the point of emission. Weaver moves the specification from prose into a machine-checkable position in the build.

## Resolving a registry

A registry is a directory of YAML models describing attribute groups, spans, metrics and events. Each entry carries a name, a type, a stability level and a requirement level. **Resolution is the operation that turns that directory into one canonical artifact**: files are flattened, `extends` relationships are applied, referenced attributes are imported, and internal consistency is validated. Every other Weaver subcommand consumes the resolved form, so a registry that fails to resolve cannot be generated from or checked against.

`registry check` performs the resolution and reports the violations it finds. It targets the public conventions by default, or a local folder:

```bash
# Validate the upstream OTel semantic conventions
weaver registry check \
  -r https://github.com/open-telemetry/semantic-conventions.git[model]

# Validate a local registry with custom policies
weaver registry check -r ./model -p ./policies
```

Running it in a container avoids installing a toolchain on continuous-integration (CI) workers; the published image is `otel/weaver`:

```bash
docker run --rm \
  -v "${PWD}/model:/home/weaver/model" \
  otel/weaver registry check -r /home/weaver/model
```

The volume mount is load-bearing: the `-r` path is resolved **inside** the container, so a host-relative path with no corresponding mount produces a missing-registry error rather than a validation result.

## Policy enforcement

Structural validity is a weaker property than organizational conformance, so Weaver separates the two. It ships a set of default policies covering registry consistency and backwards compatibility — among them rejecting conflicting attribute definitions, type changes on stable attributes, and removal of stable elements — and accepts additional rules written in [Open Policy Agent](https://www.openpolicyagent.org/) Rego and passed with `-p`.

The backwards-compatibility policies are the ones whose violations are invisible downstream. **A stable attribute whose type changes, or which disappears, breaks every consumer already querying it**, and those consumers are dashboards and alerts rather than compilers, so nothing downstream would report the break. Encoding the constraint as a check that fails the build converts a silent-at-runtime error into a rejected pull request.

Organization-specific rules take the same shape: a requirement that every new attribute declare a `stability` field, or that no attribute name exceed a fixed number of namespace segments, is a `.rego` file evaluated against the resolved registry.

## Generating type-safe code

`registry generate` runs the resolved registry through templates written in [Jinja](https://palletsprojects.com/p/jinja/) syntax to emit constants, enumerations and documentation for a target language. This is the mechanism by which OpenTelemetry produces its own per-language semantic-convention packages.

```bash
weaver registry generate \
  -r ./model \
  -t ./templates \
  go ./generated
```

The `TARGET` positional argument (`go` above) selects a template set within `-t`; the second positional argument is the output directory. Instrumentation then references a generated constant such as `semconv.HTTPRequestMethodKey` instead of the literal `"http.request.method"`. **The change relocates the error from query time to compile time**: a misspelling that previously produced an extra time series now fails to name a symbol.

The relocation is only as complete as the call sites that adopt it. A generated constant does not prevent a string literal elsewhere in the same file, so the guarantee is bounded by whatever lint or review forbids raw attribute strings.

## Live-checking real telemetry

Static checks constrain the definitions; they say nothing about what a running process puts on the wire — an instrumentation library, a Collector processor, or a manual span attribute can all emit names the registry never described. `registry live-check` closes that gap by standing up an OTLP receiver, comparing incoming signals against the resolved registry, and reporting **missing required attributes, type mismatches, deprecated attributes and invalid enumeration values**, together with a coverage score over the registry's surface.

```bash
# Receive OTLP over gRPC and report live conformance
weaver registry live-check \
  -r ./model \
  --input-source otlp \
  --otlp-grpc-port 4317 \
  --admin-port 8080
```

Pointing a service, or a Collector exporter, at this endpoint in staging or CI surfaces non-conforming telemetry before the data reaches production storage. The complementary subcommand `registry emit` generates sample OTLP from the schema, which allows dashboards and alerts to be built against the intended attribute set before any instrumentation exists.

## Position in the pipeline

The three subcommands cover disjoint stages. `check` gates the definitions, `generate` propagates them into typed libraries, and `live-check` verifies the emitted result. Stability in the upstream conventions is what makes the arrangement worth the build-time cost: a convention that is still changing shape forces regeneration and re-review on every revision.

## Pitfalls

- **A generated constant coexisting with a hand-written literal.** Symptom: the typed constant is in use, yet an off-convention series still appears. Cause: `generate` produces symbols but forbids nothing, so any remaining string literal emits exactly as before.
- **Passing a host path to a containerized `check`.** Symptom: the registry cannot be found despite the directory existing. Cause: `-r` is interpreted inside the container, and an unmounted path does not exist there.
- **Treating `registry check` as coverage of production telemetry.** Symptom: CI is green while dashboards remain partly empty. Cause: `check` validates the registry files, not the attributes a running process emits; only `live-check` observes the wire.
- **Changing the type of a stable attribute.** Symptom: existing queries return fewer or no rows without any error. Cause: consumers are dashboards and alert rules, which have no type checker; the default Weaver policy rejecting the change is the only enforcement point.
- **Reading the second positional argument of `generate` as the target.** Symptom: output lands in a directory named after the language. Cause: the first positional argument is the `TARGET` template set and the second is the output directory.
