---
title: "OpAMP: remotely managing a fleet of OpenTelemetry Collectors"
date: 2026-07-31
track: observability
summary: "Editing collector.yaml by hand on a thousand hosts does not scale. OpAMP is OpenTelemetry's protocol for remote configuration, health reporting and agent auto-updates over a single bidirectional connection. This article covers the two-message protocol shape, the remote-config hash handshake, and the Supervisor pattern that wraps the Collector so a rejected config rolls back instead of blinding the pipeline."
reading_time: 6
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

**Gist.** Beyond a handful of OpenTelemetry Collectors, configuration becomes the operational bottleneck: a sampling change or a new pipeline must reach every agent, and the operator has no direct evidence of which agents applied it, which are unhealthy, and which still run an older binary. The Open Agent Management Protocol (OpAMP) replaces image-rebuild-and-redeploy with a persistent bidirectional connection carrying two Protobuf messages, reconciled by a configuration hash so that the server always holds the agent's reported state. The cost is a second control plane to run and secure — a management server, a long-lived connection per agent, and, for write capability, an extra supervising process on every host.

## Two messages over one connection

OpAMP is deliberately small. An agent opens a persistent connection to a **management server** — WebSocket, or plain HTTP for constrained environments — and the entire protocol consists of two Protobuf messages flowing over it.

**`AgentToServer`** is the agent's report. The load-bearing fields:

- `instance_uid` — a stable 16-byte ULID/UUIDv7 identifying this agent instance. Stability across restarts is what lets the server treat reconnects as the same agent rather than a new one.
- `agent_description` — identifying attributes (service name, operating system, version) used to bucket agents into groups.
- `capabilities` — a bitmask of what the agent supports (`AcceptsRemoteConfig`, `ReportsHealth`, `AcceptsPackages`, and so on). The server must not assume a behaviour the bitmask does not advertise.
- `effective_config` — the configuration the agent is running at that moment, which is not necessarily the configuration most recently offered.
- `remote_config_status` — the outcome of the last configuration the server pushed (`APPLIED`, `APPLYING` or `FAILED`) together with the hash that outcome refers to.
- `health` — a `ComponentHealth` tree: an overall status plus per-component sub-health, so a single failing exporter is distinguishable from a dead agent.

**`ServerToAgent`** is the server's response. It carries `remote_config` (a new configuration offer), `packages_available` (binaries for auto-update), `connection_settings` and `command` (for example, restart).

### The hash handshake

Reconciliation rests on the **`config_hash`**. Every configuration the server offers carries one. The agent echoes, in `remote_config_status`, the hash of the configuration it currently has applied. The server compares the two:

- **hashes equal** — the agent is converged; the server sends no `remote_config`.
- **hashes differ** — the server sends the desired configuration, and the agent transitions `APPLYING` → `APPLIED` or `FAILED`, reporting the same hash back so the transition is unambiguous.

The invariant is that **the desired state is identified by content, not by message ordering**. A duplicated or lost `ServerToAgent` therefore does not corrupt convergence: the next report restates the applied hash and the comparison repeats. This makes the exchange idempotent and reconnect-tolerant, and removes the need for the server to hold fragile per-agent session state. The `FAILED` status is equally load-bearing: an agent that cannot apply a configuration reports the offered hash with a failure, so the server can distinguish *not yet delivered* from *delivered and rejected*.

## The Supervisor pattern

The OpenTelemetry Collector itself is not fully OpAMP-aware for lifecycle management, so the recommended deployment is the **Supervisor**: a separate process that speaks OpAMP to the server and manages a child Collector process beneath it.

The decoupling is what makes rollback possible. A Collector that is handed an invalid configuration fails at startup; a self-managing Collector would have no surviving process left to report that failure. The Supervisor outlives the child, so when a pushed configuration causes the Collector to fail to start it **reports `FAILED` upstream and reverts to the last-known-good configuration**, keeping the telemetry pipeline flowing rather than dark.

A minimal supervisor configuration names the server, declares capabilities, and names the Collector binary to run:

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

Invoked as `opampsupervisor --config supervisor.yaml`, it merges any server-pushed configuration with a local base, writes the effective configuration, and starts or restarts the Collector. The `storage.directory` is where the Supervisor persists state across its own restarts, including the agent's `instance_uid` and the last remote configuration it received. The Supervisor remains an **alpha**-stability component even though the wire protocol is at **Beta** maturity; `opamp-go` is the reference implementation of the protocol.

## Read-only and read-write participation

A Collector can participate in two ways, distinguished by which capability bits are set.

The lighter path is the **`opampextension`**, configured inside the Collector's own `extensions:` block. It reports health and effective configuration to the server but does not accept remote configuration — observation without control, and no second process on the host.

The **Supervisor** path adds write capability: remote configuration, package management and auto-updates. Auto-update rides the same connection. When the server advertises `packages_available` with a new binary, a hash and a download URL, an agent that declared `AcceptsPackages` downloads the package, verifies the hash, swaps the binary, and reports progress through `package_statuses`. The rollout is therefore confirmed per agent through the same report channel as configuration, without an external configuration-management tool.

### Implementation sketch (Scala)

The server side of the reconciliation is a pure function of the agent's report and the desired configuration for its group. The connection, Protobuf codec and storage are omitted.

```scala
enum RemoteConfigStatus:
  case Applied, Applying, Failed

final case class AgentReport(
    instanceUid: String,
    appliedHash: Vector[Byte],
    status: RemoteConfigStatus,
    capabilities: Long
)

final case class DesiredConfig(body: Array[Byte], hash: Vector[Byte])

enum ServerAction:
  case Converged
  case Offer(config: DesiredConfig)
  case Quarantine(hash: Vector[Byte])   // agent rejected this content

val AcceptsRemoteConfig: Long = 1L << 1   // capability bit, per the spec

def reconcile(report: AgentReport, desired: DesiredConfig): ServerAction =
  if (report.capabilities & AcceptsRemoteConfig) == 0 then ServerAction.Converged
  else if report.appliedHash == desired.hash then
    report.status match
      // A FAILED status carrying the desired hash means the content itself is
      // bad; re-offering it would loop the agent through the same failure.
      case RemoteConfigStatus.Failed => ServerAction.Quarantine(desired.hash)
      case _                         => ServerAction.Converged
  else if report.status == RemoteConfigStatus.Applying then ServerAction.Converged
  else ServerAction.Offer(desired)
```

The state carried across reconnects is the hash pair alone, so a server restart loses nothing that the next `AgentToServer` does not restore.

**Reproduction.** Build `opampsupervisor` from `opentelemetry-collector-contrib` and point it at the example OpAMP server shipped with the `opamp-go` reference implementation, whose local endpoint the supervisor README records. Pushing a processor-setting change from the server exercises the apply path; pushing a deliberately invalid configuration exercises the `FAILED` report and the revert to last-known-good.

## Pitfalls

- **A server that re-offers a configuration whose hash the agent has already reported `FAILED` puts that agent in a restart loop**: the reconciliation compares hashes but ignores status, so the same rejected content is delivered indefinitely.
- **An `instance_uid` regenerated on every restart makes the fleet inventory grow without bound**, because the server has no way to recognise the restarted agent as the same instance.
- **Acting on a capability the agent never advertised produces silent no-ops**: an agent without `AcceptsPackages` ignores `packages_available`, and the rollout appears stalled rather than refused.
- **Deploying the `opampextension` when configuration push is the goal yields health and effective-config reporting only**; the extension does not accept remote configuration, so pushes have no effect.
- **Treating `effective_config` as confirmation that the last offer took hold conflates two facts**: the field reports what is running, which after a rollback is the previous configuration, not the offered one.
- **Losing the Supervisor's `storage.directory` discards the last-known-good configuration**, so a rejected push after that loss has nothing to revert to.
- **`insecure_skip_verify: true` on the supervisor's TLS block turns the control plane into an unauthenticated remote-configuration channel**, since remote config is arbitrary executable pipeline configuration.
- **The Supervisor is an alpha-stability component while the protocol is Beta**; stability expectations set by the wire protocol do not transfer to the process that implements the write path.
