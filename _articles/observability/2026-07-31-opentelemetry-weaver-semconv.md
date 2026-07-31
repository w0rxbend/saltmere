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

## The drift problem

`http.request.method` is a contract. A dashboard filters on it, an alert groups by it, a trace query joins on it. But nothing stops service A from emitting `http.method`, service B from typoing `http.reqest.method`, and a third team from inventing `http_request_method` because someone used a metric-style separator. Each variant silently produces a new time series. Dashboards go half-blank, alerts stop firing, and nobody notices until an incident.

OpenTelemetry's semantic conventions define the correct names — hundreds of attributes across dozens of domains — but a spec in a Markdown table doesn't enforce anything at the point where telemetry is emitted. Weaver closes that gap by treating the convention registry as a compilable API: something you validate in CI, generate code from, and check live traffic against.

## Resolving a registry

A registry is a directory of YAML models: attribute groups, spans, metrics, events, each with a name, type, stability level, and requirement level. Weaver's first job is *resolution* — flattening files, applying `extends`, importing referenced attributes, and validating internal consistency into one canonical artifact.

`registry check` runs that resolution and reports violations. It targets the public conventions by default, or a local folder:

```bash
# Validate the upstream OTel semantic conventions
weaver registry check \
  -r https://github.com/open-telemetry/semantic-conventions.git[model]

# Validate your own registry with custom policies
weaver registry check -r ./model -p ./policies
```

Run it in Docker so CI needs no toolchain install (image `otel/weaver`):

```bash
docker run --rm \
  -v "${PWD}/model:/home/weaver/model" \
  otel/weaver registry check -r /home/weaver/model
```

## Policy enforcement

Structural validity isn't enough; organizations want *conventions about conventions*. Weaver ships default policies (no duplicate attributes, no type changes on stable attributes, namespace-collision prevention, no removal of stable elements) and lets you add your own as [Open Policy Agent](https://www.openpolicyagent.org/) Rego. A rule like "every new attribute must carry a `stability` field" or "no attribute name may exceed three namespace segments" becomes a `.rego` file passed with `-p`. A failing check breaks the build, so a misspelled or off-convention attribute never merges.

## Generating type-safe code

The payoff for developers: you never hand-type an attribute string again. `registry generate` runs the resolved registry through [Jinja2](https://palletsprojects.com/p/jinja/) templates to emit constants, enums, and docs in any target language. This is exactly how OTel produces its official per-language semconv packages.

```bash
weaver registry generate \
  -r ./model \
  -t ./templates \
  go ./generated
```

The `TARGET` (`go` here) selects a template set; the second positional argument is the output directory. Now instead of `span.SetAttribute("http.request.method", ...)`, code references a generated `httpconv.RequestMethod` constant. A typo becomes a compile error rather than a broken dashboard three weeks later.

## Live-checking real telemetry

Static checks guard the definitions; `registry live-check` guards what's actually on the wire. It stands up an OTLP receiver, compares incoming signals against the registry, and reports missing required attributes, type mismatches, deprecated attributes, and invalid enum values — plus a coverage score, like test coverage but for your telemetry surface.

```bash
# Receive OTLP over gRPC and report live conformance
weaver registry live-check \
  -r ./model \
  --input-source otlp \
  --otlp-grpc-port 4317 \
  --admin-port 8080
```

Point a service (or the Collector) at it in staging or CI and catch non-conforming telemetry before it reaches production. There's also `registry emit`, which generates sample OTLP from the schema so you can build dashboards and alerts *before* the instrumentation exists — dissolving the usual chicken-and-egg wait.

## Why now

Semantic conventions are steadily reaching stability, and stable conventions are only useful if they're mechanically enforced. Weaver makes the registry the single source of truth: `check` gates the definitions, `generate` pushes them into type-safe libraries, and `live-check` verifies the emitted reality. Attribute drift stops being a naming-discipline problem and becomes a build failure.

**Try next:** `docker run --rm otel/weaver registry check` against the upstream `open-telemetry/semantic-conventions` repo, then write a small Rego policy that requires every attribute to declare a `stability` field and watch the check fail on a violation you introduce.
