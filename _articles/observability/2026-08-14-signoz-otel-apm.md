---
title: "SigNoz: A Self-Hostable OpenTelemetry-Native APM"
date: 2026-08-14
track: observability
summary: "SigNoz is an open-source, OpenTelemetry-native application performance monitoring system that stores traces, metrics, logs, and exceptions in a single ClickHouse backend, ingesting everything over OTLP with no vendor agent."
reading_time: 6
tags: [signoz, opentelemetry, otlp, clickhouse, apm, observability]
sources:
  - title: "SigNoz — Docker self-host install (official docs)"
    url: "https://signoz.io/docs/install/docker/"
  - title: "SigNoz/signoz — GitHub"
    url: "https://github.com/SigNoz/signoz"
  - title: "SigNoz LICENSE (MIT Expat + enterprise directories)"
    url: "https://github.com/SigNoz/signoz/blob/main/LICENSE"
  - title: "SigNoz as a self-hosted OpenTelemetry backend (OneUptime, Feb 2026)"
    url: "https://oneuptime.com/blog/post/2026-02-06-signoz-self-hosted-opentelemetry-backend/view"
---

**Gist.** A self-hosted observability stack is normally assembled from separate stores — one for metrics, one for traces, one for logs — each with its own query language, so correlating a slow request with its log lines happens by hand at the dashboard layer. SigNoz replaces the assembly with **one ingestion protocol (OpenTelemetry Protocol, OTLP) and one storage engine (ClickHouse)**, giving a single SQL query surface over all signals. The cost is a heavyweight stateful dependency: ClickHouse plus its coordination service must be operated, memory-provisioned, and retained, and the metrics ecosystem around it is younger than the Prometheus one it displaces.

## OpenTelemetry-native rather than OpenTelemetry-compatible

The distinction is architectural, not marketing. Many application performance monitoring (APM) products define a proprietary agent and wire format, then bolt on an OTLP receiver as a secondary path; signals arriving through that path are translated into the internal model and can lose fidelity where the models disagree. In SigNoz, **OTLP is the native ingestion protocol**: OpenTelemetry data is stored in the form it arrives in, and there is no proprietary wire format and no vendor software development kit (SDK) to adopt. Other receivers can still be enabled on the bundled collector, but they are conversions into the OpenTelemetry model rather than the primary path.

Ingestion is performed by a bundled SigNoz OpenTelemetry Collector listening on the standard OTLP ports — **4317 for OTLP over gRPC and 4318 for OTLP over HTTP**. Traces, metrics, and logs share those ports; the signal type is carried by the request path or gRPC service, not by a separate daemon per signal. An already-instrumented service therefore moves to SigNoz by changing endpoint configuration alone:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4317"
export OTEL_EXPORTER_OTLP_PROTOCOL="grpc"
export OTEL_SERVICE_NAME="checkout-api"
```

The same property makes SigNoz composable rather than exclusive. Because the wire format is stock OTLP, an existing OpenTelemetry Collector can remain in front as the aggregation tier and **list SigNoz as one exporter among several**, fanning the same pipeline out to an incumbent backend during migration. That removes the usual cutover risk: both systems observe identical data from identical instrumentation, so a comparison is a direct one rather than an argument about agent differences.

## Why the storage engine is columnar

Spans and log records are wide, high-cardinality, append-only events. Each carries a large and irregular attribute set — service, endpoint, version, tenant, region, status code — and is never updated after write. That shape matches an online analytical processing (OLAP) column store closely on three counts.

First, **queries touch few columns of many rows**. A question such as "99th-percentile latency by endpoint by version over the last six hours" reads a timestamp, a duration, and two attributes; a column store reads only those columns' data blocks and skips the rest of each event entirely, whereas a row store must fault in whole records. Second, **columnar compression exploits the low local entropy of each column**. Service names, status codes, and versions repeat heavily within a block, so the compressed footprint is far smaller than the logical event size — the property that makes retaining rich, wide events affordable rather than forcing attribute pruning at instrumentation time. Third, aggregation over a column runs as a vectorised scan rather than per-row interpretation.

The consequence that matters operationally is uniformity of interface. **One engine and one dialect of SQL cover all three signals**, in place of PromQL for metrics, TraceQL for traces, and LogQL for logs joined only visually on a dashboard. Exceptions are stored as first-class events: stack traces are grouped and each group links back to the trace that produced it, so the path from an error aggregate to the individual slow request is a stored relation rather than a manual timestamp search.

## Standing up a self-hosted instance

The self-host path is a repository clone followed by a Docker Compose start. Releases are frequent, and the version deployed is whatever the checked-out revision of the repository pins in its Compose file rather than a separately chosen number.

```bash
git clone -b main https://github.com/SigNoz/signoz.git
cd signoz/deploy/docker
docker compose up -d --remove-orphans
```

Startup is asynchronous: the collector accepts connections before ClickHouse has finished schema initialisation, so the logs are the authoritative readiness signal rather than the port being open.

```bash
docker compose logs -f --tail=50
# UI at http://localhost:8080
```

**Docker must be given at least 4 GB of memory**; ClickHouse and ZooKeeper both require headroom, and an under-provisioned daemon typically surfaces as a container killed by the out-of-memory reaper under load rather than as a clean startup error. For clustered deployment a Helm chart is published; the OTLP endpoints are unchanged and only the target host differs, becoming the collector service's cluster DNS name instead of `localhost`.

## Licensing

SigNoz is open-core, and the boundary is drawn by directory rather than by feature flag. The main repository is licensed **MIT (Expat) for everything outside the `ee/` and `cmd/enterprise/` directories**; code inside those two directories is covered by a separate SigNoz Enterprise license. The self-hosted traces, metrics, and logs platform falls under MIT; enterprise-only capabilities such as single sign-on (SSO) and finer-grained role-based access control (RBAC) sit behind the enterprise license.

A permissive core distinguishes SigNoz from projects that relicensed under the Business Source License (BSL) or Server Side Public License (SSPL). The boundary is nonetheless defined by the `LICENSE` file of a specific revision, and that file is the artefact to check before building a commercial product on a given version — a directory-scoped split can move between releases in a way a single top-level license identifier cannot express.

## Where it fits

SigNoz answers trace, metric, and log questions from one store with SQL and without per-host billing. The countervailing facts are ecosystem age: **its metrics functionality is younger than Prometheus's**, and there is no equivalent of Grafana's accumulated community dashboard library, so a team whose operational knowledge is encoded in PromQL expressions and imported dashboards carries a real migration cost. For a team already emitting OpenTelemetry, the OTLP-in, ClickHouse-out design requires no instrumentation change at all, which makes the evaluation cheap to run: deploy the stack, point one instrumented service at it, generate traffic, and measure the elapsed time to locate the slowest endpoint's 99th-percentile latency by version. That time-to-answer, measured against the incumbent stack on identical data, is the comparison that is not confounded by agent differences.

## Pitfalls

- **A container exits under load when Docker is given less than the documented 4 GB minimum.** The shortfall surfaces as an out-of-memory kill rather than as a startup failure, so the stack appears to come up correctly first.
- **The user interface is reachable but shows no services immediately after `docker compose up`.** The collector binds its ports before ClickHouse schema initialisation completes; readiness must be read from the Compose logs, not inferred from an open port.
- **An exporter configured for port 4317 while set to the HTTP protocol fails to deliver.** 4317 carries OTLP over gRPC and 4318 carries OTLP over HTTP; `OTEL_EXPORTER_OTLP_PROTOCOL` and the port must agree.
- **A Helm-based deployment silently receives nothing when the endpoint is left at `localhost`.** In-cluster exporters must target the collector service's DNS name; `localhost` resolves to the emitting pod, which has no receiver.
- **A commercial derivative assumes the whole repository is MIT.** The `ee/` and `cmd/enterprise/` directories are under the separate SigNoz Enterprise license, and the split is per revision, so the `LICENSE` file of the deployed version governs.
- **SSO and granular RBAC are absent from a self-hosted deployment that was expected to have them.** Those are enterprise-licensed features, not configuration of the MIT core.
