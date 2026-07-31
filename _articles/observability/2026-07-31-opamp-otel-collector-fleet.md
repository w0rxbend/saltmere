---
title: "OpAMP: remotely managing a fleet of OpenTelemetry Collectors"
date: 2026-07-31
track: observability
summary: "Editing collector.yaml by hand on a thousand hosts does not scale. OpAMP is OpenTelemetry's protocol for remote config, health reporting, and agent auto-updates over a single bidirectional connection. Here is the two-message protocol shape, the remote-config hash handshake, and the Supervisor pattern that wraps the Collector so a bad config rolls back instead of blinding your pipeline."
reading_time: 5
tags: [opamp, opentelemetry, otel-collector, fleet-management, supervisor, remote-config]
sources:
  - title: "Open Agent Management Protocol (OpAMP) specification — open-telemetry/opamp-spec"
    url: "https://github.com/open-telemetry/opamp-spec/blob/main/specification.md"
  - title: "Open Agent Management Protocol — OpenTelemetry docs"
    url: "https://opentelemetry.io/docs/specs/opamp/"
  - title: "OpAMP Supervisor for the OpenTelemetry Collector (contrib README)"
    url: "https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/cmd/opampsupervisor/README.md"
  - title: "Operating OpenTelemetry at scale with OpAMP (CNCF, Jul 2026)"
    url: "https://www.cncf.io/blog/2026/07/13/operating-opentelemetry-at-scale-with-opamp/"
---

Once you run more than a handful of OpenTelemetry Collectors, configuration becomes the operational bottleneck. Every sampling change, every new pipeline, every processor tweak has to reach every agent — and you need to know which agents actually applied it, which are unhealthy, and which are still running last quarter's binary. Baking `collector.yaml` into an image and redeploying is slow and gives you no feedback loop. **OpAMP (Open Agent Management Protocol)** is OpenTelemetry's answer: an open, vendor-neutral control plane protocol for managing a fleet of agents remotely. The spec is currently at **Beta** maturity, with `opamp-go` as the reference implementation.

## Two messages, one connection

OpAMP is deliberately small. An agent opens a persistent connection to a **management server** (WebSocket, or plain HTTP for constrained environments) and the entire protocol is two Protobuf messages flowing over it.

**`AgentToServer`** is the agent's report. The important fields:

- `instance_uid` — a stable 16-byte ULID/UUIDv7 identifying this agent instance.
- `agent_description` — identifying attributes (service name, OS, version) used to bucket agents into groups.
- `capabilities` — a bitmask of what the agent supports (`AcceptsRemoteConfig`, `ReportsHealth`, `AcceptsPackages`, and so on).
- `effective_config` — the configuration the agent is *actually running* right now.
- `remote_config_status` — the result of the last config the server pushed (`APPLIED`, `APPLYING`, or `FAILED`) plus the hash it applied.
- `health` — a `ComponentHealth` tree: overall status plus per-component sub-health.

**`ServerToAgent`** is the server's response, carrying `remote_config` (a new config offer), `packages_available` (binaries for auto-update), `connection_settings`, and `command` (e.g. restart).

The clever part is the **remote config hash**. Every config the server offers carries a `config_hash`. The agent echoes back the hash it currently has applied in `remote_config_status`. The server compares hashes: if they match, nothing to do; if they differ, it sends the new config. This makes the exchange idempotent and stateless-friendly — reconnects and missed messages self-heal, and the server always knows the true state of every agent without keeping a fragile session.

## The Supervisor pattern

The OpenTelemetry Collector itself is not fully OpAMP-aware for lifecycle management, so the recommended deployment is the **Supervisor**: a small process that speaks OpAMP to the server and manages a child Collector process underneath it. This decoupling matters. If the server pushes a config the Collector rejects, the Supervisor catches the failed start, reports `FAILED` upstream, and **reverts to the last-known-good config** so your telemetry pipeline keeps flowing instead of going dark.

A minimal supervisor config points at the server, declares its capabilities, and names the Collector binary to run:

```yaml
# supervisor.yaml
server:
  endpoint: wss://opamp.example.com/v1/opamp
  headers:
    Authorization: "Bearer ${OPAMP_TOKEN}"
  tls:
    insecure_skip_verify: false

capabilities:
  accepts_remote_config: true
  reports_effective_config: true
  reports_own_metrics: true
  reports_health: true
  reports_remote_config: true

agent:
  executable: /usr/bin/otelcol-contrib
  config_apply_timeout: 30s
  bootstrap_timeout: 5s

storage:
  directory: /var/lib/otelcol/supervisor
```

Run it with `opampsupervisor --config supervisor.yaml`; it merges any server-pushed config with a local base, writes the effective config, and (re)starts the Collector. Note the Supervisor is still an **alpha**-stability component even though the wire protocol is Beta.

## Read-only vs. read-write

There are two ways a Collector participates. The lighter path is the **`opampextension`** configured inside the Collector's own `extensions:` block — it reports health and effective config to the server but does *not* accept remote config. That is the read-only, observe-my-fleet mode. The full **Supervisor** path adds write capability: remote config, package management, and auto-updates. Pick the extension when you only want visibility; pick the Supervisor when you want to actually drive configuration and binary versions from the control plane.

Auto-update rides the same channel. When the server advertises `packages_available` with a new binary, a hash, and a download URL, an agent that declared `AcceptsPackages` downloads it, verifies the hash, swaps the binary, and reports progress through `package_statuses` — a full rollout with per-agent confirmation, no external config-management tool required.

**Try next:** Clone `opentelemetry-collector-contrib`, run the bundled example OpAMP server, and start `opampsupervisor` with a config pointing at `ws://127.0.0.1:4320/v1/opamp`. Push a config change that flips a processor setting from the server UI and watch the Supervisor apply it, then push a deliberately invalid config and confirm it reports `FAILED` and rolls back to the last-known-good instead of crashing the Collector.
