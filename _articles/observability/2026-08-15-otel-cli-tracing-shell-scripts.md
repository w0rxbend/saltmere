---
title: "Tracing Shell Scripts, Cron Jobs, and CI Pipelines with otel-cli"
date: 2026-08-15
track: observability
summary: "A nightly backup script and a 14-minute CI job have step structure, nesting and failure modes, but report only exit codes. otel-cli wraps any command in an OpenTelemetry Protocol span and propagates trace context through the TRACEPARENT environment variable the way HTTP headers carry it between services. This article covers the span/exec model, collector wiring, and the raw-curl fallback for hosts where no binary may be installed."
reading_time: 6
tags: [opentelemetry, otel-cli, tracing, shell, ci-cd, cron, otlp]
sources:
  - title: "equinix-labs/otel-cli — OpenTelemetry command-line tool (GitHub)"
    url: "https://github.com/equinix-labs/otel-cli"
  - title: "otel-cli releases (v0.4.x)"
    url: "https://github.com/equinix-labs/otel-cli/releases"
  - title: "OTLP Specification (opentelemetry.io)"
    url: "https://opentelemetry.io/docs/specs/otlp/"
  - title: "opentelemetry-proto — OTLP/JSON curl examples (GitHub)"
    url: "https://github.com/open-telemetry/opentelemetry-proto/blob/main/examples/README.md"
  - title: "Honeycomb — Send a Test Span Through an OpenTelemetry Collector"
    url: "https://www.honeycomb.io/blog/test-span-opentelemetry-collector"
---

**Gist.** Batch work — cron jobs, Makefiles, continuous integration (CI) pipelines — has the same internal structure as a distributed request (nested steps, durations, failures) but emits only an exit code, so a step that grew from four minutes to fourteen is invisible. [otel-cli](https://github.com/equinix-labs/otel-cli) wraps a command in an OpenTelemetry Protocol (OTLP) span and relays trace context to child processes through the `TRACEPARENT` environment variable, making the process tree a trace tree. The cost is one short-lived OTLP connection per invocation and a dependency on an external binary; the deployment the project documents is a collector on localhost rather than a remote backend.

The corpus [first trace](/articles/observability/2026-07-24-opentelemetry-first-trace/) came from an instrumented application. otel-cli, from Equinix Labs, addresses the complementary gap: a single Go binary that emits OTLP spans from shell. It is a small project, tagged in the v0.4.x series, and its activity merits inspection before a platform depends on it, though the propagation pattern it implements outlives any one binary.

## Spans from the shell

The central verb is `exec`: run a command, wrap it in a span, record duration and exit status.

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=grpc://localhost:4317
export OTEL_SERVICE_NAME=nightly-backup

otel-cli exec --name "rsync to nas" -- \
    rsync -a /var/lib/influxdb/ nas:/backups/influxdb/
```

`otel-cli span` is the non-wrapping variant: it takes explicit start and end timestamps and reports a phase that has already completed (`--start "$start_ts" --end "$(date --rfc-3339=ns)"`). For long-running scripts, `otel-cli span background` holds a span open in a background process and accepts `otel-cli span event` calls from the rest of the script, so a twenty-minute job becomes **one span decorated with events** ("finished dump", "starting upload") instead of an opaque interval.

Two behaviours make the wrapper safe to apply broadly. First, **an unconfigured otel-cli is a no-op**: with no endpoint set it emits no error and the wrapped command runs normally, which the project states as a design goal. Second, **`exec` passes the child's exit code through**, so wrapping does not alter script semantics or break `set -e` logic that inspects the status.

## TRACEPARENT: the pipeline is the propagation

The property that separates this from step timing is context propagation. Between Hypertext Transfer Protocol (HTTP) services, trace context travels in the `traceparent` header ([W3C Trace Context is covered here](/articles/observability/2026-07-31-w3c-trace-context-propagation/)); between processes in a shell it travels in the `TRACEPARENT` environment variable, in the same `00-traceid-spanid-01` form. otel-cli **reads `TRACEPARENT` if present and parents its span to it, and `exec` injects a freshly generated value into the child environment**. Nesting follows from the two halves of that rule without further configuration.

```bash
#!/usr/bin/env bash
# backup.sh — one trace, three child spans
export OTEL_SERVICE_NAME=nightly-backup
export OTEL_EXPORTER_OTLP_ENDPOINT=grpc://localhost:4317

otel-cli exec --name "nightly-backup" -- bash -c '
    otel-cli exec --name "influxdb dump"  -- influxd backup -portable /tmp/dump
    otel-cli exec --name "rsync to nas"   -- rsync -a /tmp/dump nas:/backups/
    otel-cli exec --name "verify + prune" -- ./verify_and_prune.sh
'
```

The outer `exec` creates the root span and exports `TRACEPARENT`; each inner `exec` inherits it and becomes a child. The invariant is that **a process sees exactly the context of its nearest enclosing `exec`** — inheritance is by process ancestry, not by textual position in the script. A step launched with `&` and reaped later still carries the context it was forked with; a step launched from a shell that never inherited the variable starts a new trace.

The relay does not stop at process boundaries that speak OTLP. Any downstream participant that speaks W3C Trace Context joins the same trace: if `verify_and_prune.sh` calls a health endpoint with `-H "traceparent: $TRACEPARENT"`, the application's spans attach to the cron job's trace, producing a single trace covering cron firing, script phases, the HTTP call into the service, and the database query beneath it.

The same mechanism traces a Makefile or a CI job. In GitHub Actions, a `TRACEPARENT` generated in the first step and written to `$GITHUB_ENV` is visible to subsequent steps, each of which wraps its phase (`otel-cli exec --name "idf.py build" -- idf.py build`); the waterfall then attributes the fourteen minutes to a specific phase. CI-specific wrappers exist that automate the bookkeeping, but they implement the same environment-variable relay.

## Collector wiring

otel-cli **opens a fresh OTLP connection on each invocation**, and the README recommends pointing it at a **local collector** — an [OpenTelemetry Collector or Grafana Alloy](/articles/observability/2026-07-26-grafana-alloy-collector/) on localhost — which buffers and batches toward the real backend. The arrangement also bounds the blast radius of a backend outage: spans are dropped or queued at the collector rather than the backup blocking on a remote endpoint.

Configuration uses the standard environment-variable surface: `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_PROTOCOL` (gRPC versus HTTP/protobuf), `OTEL_SERVICE_NAME`, and the header variables for authenticated endpoints. For debugging, `otel-cli server tui` runs a throwaway OTLP receiver in the terminal and prints arriving spans, which verifies span structure without involving a backend.

## The no-install fallback: curl straight to OTLP/HTTP

OTLP defines a [JSON encoding over HTTP](https://opentelemetry.io/docs/specs/otlp/), so on a host where no binary may be installed a span reduces to one POST to the collector's port 4318. The [opentelemetry-proto examples](https://github.com/open-telemetry/opentelemetry-proto/blob/main/examples/README.md) provide a ready-made `trace.json`.

```bash
# Minimal hand-rolled span: 16-byte trace ID, 8-byte span ID, nanosecond bounds.
trace_id=$(openssl rand -hex 16)
span_id=$(openssl rand -hex 8)
start=$(date +%s%N)
rsync -a /tmp/dump nas:/backups/
end=$(date +%s%N)

sed -e "s/TRACE_ID/$trace_id/" -e "s/SPAN_ID/$span_id/" \
    -e "s/START/$start/" -e "s/END/$end/" span.tmpl.json > span.json

curl -s -X POST http://collector:4318/v1/traces \
  -H "Content-Type: application/json" \
  --data-binary @span.json
```

Trace and span identifiers are 16 and 8 random bytes respectively, rendered as hexadecimal, and OTLP timestamps are nanoseconds since the Unix epoch; both are obtainable in shell from `openssl rand -hex` and `date +%s%N`. This is the appropriate escape hatch for appliances and containers outside administrative control, and it exposes what otel-cli performs on the caller's behalf. Between the two extremes sit the OpenTelemetry software development kits (SDKs): once a script has acquired substantive logic, reimplementing it in a language with an SDK is preferable to extending the shell version.

Once cron and CI emit spans, the remainder of the stack applies unchanged: [span metrics](/articles/observability/2026-07-30-spanmetrics-connector-red-metrics/) yield duration histograms per job step, and an alert on the absence of a `nightly-backup` trace for 26 hours detects the failure mode exit codes cannot report — the job that never ran.

## Pitfalls

- **A step run through `ssh` or `sudo` loses its parent and starts a new trace.** Both scrub the environment by default, so `TRACEPARENT` does not reach the remote or elevated process; it must be passed explicitly (`sudo TRACEPARENT="$TRACEPARENT" …`, or `SendEnv`/`AcceptEnv` for `ssh`).
- **A stale `TRACEPARENT` exported once in a login profile parents every future job to a dead span**, producing one enormous trace that accumulates unrelated runs. The variable is per-invocation state, not configuration.
- **Pointing otel-cli directly at a remote backend adds connection setup to every wrapped command**, because a new OTLP connection is opened per invocation; a script wrapping many short steps pays that cost repeatedly.
- **Exporting `OTEL_EXPORTER_OTLP_ENDPOINT` but leaving the collector down yields no spans and no complaint from the script**, since a failed export does not fail the wrapped command: `exec` still passes the child's exit code through. Absence of telemetry is not evidence the job succeeded, which is the argument for alerting on missing traces rather than on failed ones.
- **`otel-cli span background` leaves the span open if the script exits on an error path before the closing call**, so the interval is either never exported or reported with the wrong end time; the close belongs in a `trap`.
- **`otel-cli span` accepts arbitrary `--start`/`--end` values**, so a timestamp captured in the wrong unit or timezone produces a span placed elsewhere on the timeline rather than a validation error.
