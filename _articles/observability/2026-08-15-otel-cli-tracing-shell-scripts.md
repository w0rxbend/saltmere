---
title: "Tracing Shell Scripts, Cron Jobs, and CI Pipelines with otel-cli"
date: 2026-08-15
track: observability
summary: "The nightly backup script and the 14-minute CI job are distributed systems too — they just report failures via exit codes and vibes. otel-cli wraps any command in an OTLP span, propagates TRACEPARENT through pipelines the same way HTTP headers do between services, and turns a Makefile into a flame graph. Here's the span/exec model, the collector wiring, and the raw-curl fallback when you can't install anything."
reading_time: 5
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

The corpus [first trace](/articles/observability/2026-07-24-opentelemetry-first-trace/) came from an instrumented application. But a lot of what actually breaks at 3 a.m. isn't application code — it's the cron job that rsyncs backups, the `make flash` that compiles firmware, the CI pipeline where "build" quietly grew from 4 to 14 minutes. These are processes with structure (steps, nesting, durations, failures) and zero telemetry. [otel-cli](https://github.com/equinix-labs/otel-cli) from Equinix Labs exists for exactly this gap: a single Go binary that emits OTLP spans from shell scripts. It's a small project — latest tagged release v0.4.x, and worth checking the repo's activity before betting a platform on it — but the tool is essentially feature-complete for what it does, and the pattern it implements outlives any one binary.

## Spans from the shell

The core verb is `exec`: run a command, wrap it in a span, record the duration and exit status.

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=grpc://localhost:4317
export OTEL_SERVICE_NAME=nightly-backup

otel-cli exec --name "rsync to nas" -- \
    rsync -a /var/lib/influxdb/ nas:/backups/influxdb/
```

`otel-cli span` is the non-wrapping variant — you give it explicit start/end times, useful for reporting a phase that already happened (`--start "$start_ts" --end "$(date --rfc-3339=ns)"`). For long scripts there's `otel-cli span background`, which holds a span open in a background process and accepts `otel-cli span event` calls from the rest of the script, so a 20-minute job becomes one span decorated with "finished dump", "starting upload" events rather than a blind gap.

Two design choices make it safe to sprinkle everywhere. First, unconfigured otel-cli is a no-op — no endpoint, no error, the wrapped command runs normally ("first, do no harm"). Second, `exec` passes through the child's exit code, so wrapping doesn't change script semantics.

## TRACEPARENT: the pipeline is the propagation

What elevates this from timers-with-extra-steps to *tracing* is context propagation. Between HTTP services, trace context rides the `traceparent` header ([covered for W3C Trace Context here](/articles/observability/2026-07-31-w3c-trace-context-propagation/)); between processes in a shell, it rides the `TRACEPARENT` environment variable — same `00-traceid-spanid-01` format. otel-cli reads it if present and parents its span accordingly, and `exec` injects a fresh one into the child's environment. Nesting therefore just works:

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

The outer `exec` creates the root span and exports `TRACEPARENT`; each inner `exec` becomes a child. Anything downstream that speaks W3C trace context joins the same trace — if `verify_and_prune.sh` curls a health endpoint with `-H "traceparent: $TRACEPARENT"`, your *application's* spans attach to the *cron job's* trace. That's the genuinely useful trick: one trace spanning "cron fired → script phases → HTTP call into the service → database query".

The same mechanism traces a Makefile or CI job. In GitHub Actions, generate a `TRACEPARENT` in the first step, write it to `$GITHUB_ENV`, and wrap each build phase (`otel-cli exec --name "idf.py build" -- idf.py build`); each step parents to the workflow root and the waterfall view shows exactly which phase ate the 14 minutes. Several CI-specific wrappers exist that automate this, but they're all sugar over the same env-var relay.

## Collector wiring

otel-cli opens a fresh OTLP connection per invocation, so the README's advice is to point it at a **local collector** — an [OpenTelemetry Collector or Grafana Alloy](/articles/observability/2026-07-26-grafana-alloy-collector/) on localhost that buffers and batches toward your real backend. This also decouples cron jobs from backend outages: worst case, spans drop at the collector, not block the backup. Config is the standard env-var surface (`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_PROTOCOL` for grpc vs http/protobuf, `OTEL_SERVICE_NAME`, header vars for authenticated endpoints). For debugging there's a delightful `otel-cli server tui` that runs a throwaway OTLP receiver in your terminal and pretty-prints whatever spans arrive — the fastest way to check your script's span structure before involving a real backend.

## The no-install fallback: curl straight to OTLP/HTTP

OTLP has a [JSON encoding over HTTP](https://opentelemetry.io/docs/specs/otlp/), so on a box where you can't drop a binary, a span is one POST to the collector's 4318 port — the [opentelemetry-proto examples](https://github.com/open-telemetry/opentelemetry-proto/blob/main/examples/README.md) ship a ready-made `trace.json`:

```bash
curl -s -X POST http://collector:4318/v1/traces \
  -H "Content-Type: application/json" \
  --data-binary @span.json
```

Generating valid trace/span IDs (16/8 random bytes, hex) and nanosecond timestamps in shell is ugly but entirely doable with `openssl rand -hex` and `date +%s%N`; it's the right escape hatch for appliances and containers you don't control, and understanding it demystifies what otel-cli is doing for you. Between the two extremes sit the OTel SDKs — once a "script" has grown real logic, promoting it to Python with the real SDK beats heroic bash.

Once cron and CI emit spans, the rest of the stack applies unchanged: [span metrics](/articles/observability/2026-07-30-spanmetrics-connector-red-metrics/) give you duration histograms per job step, and an alert on "nightly-backup trace absent for 26 hours" catches the failure mode exit codes never will — the job that didn't run at all.

**Try next:** wrap your ugliest cron job's phases in `otel-cli exec`, point it at a local collector, and pull up the waterfall after three nights — then add `traceparent` to one curl inside it and watch the application spans join the job's trace.
