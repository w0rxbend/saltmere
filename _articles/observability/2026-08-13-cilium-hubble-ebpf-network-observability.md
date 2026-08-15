---
title: "Hubble: eBPF network flow visibility for Kubernetes without sidecars"
date: 2026-08-13
track: observability
summary: "Cilium's Hubble reads events from the same eBPF programs that already enforce network policy, emitting L3/L4/L7 flow records, a service dependency map, and Prometheus metrics without injecting proxies into pods or changing application code."
reading_time: 5
tags: [cilium, hubble, ebpf, kubernetes, network-observability]
sources:
  - title: "Network Observability with Hubble — Cilium docs"
    url: "https://docs.cilium.io/en/stable/observability/hubble/"
  - title: "Inspecting Network Flows with the CLI"
    url: "https://docs.cilium.io/en/stable/observability/hubble/hubble-cli/"
  - title: "Setting up Hubble Observability"
    url: "https://docs.cilium.io/en/stable/observability/hubble/setup/"
  - title: "Hubble metrics reference"
    url: "https://docs.cilium.io/en/stable/observability/metrics/"
  - title: "Cilium project (GitHub)"
    url: "https://github.com/cilium/cilium"
---

**Gist.** Answering "which connection was dropped, and by whose policy" in a Kubernetes cluster normally requires either a sidecar proxy on the request path or packet capture correlated by hand against a churning pod-to-IP mapping. Hubble instead reads flow events from the extended Berkeley Packet Filter (eBPF) programs that Cilium's datapath already runs in the kernel to route, load-balance and enforce policy, so every connection is observed without a sidecar proxy, a library, or an application change. The cost is that observation is confined to what the datapath already parses: layer 3 and layer 4 (L3/L4) flows come for free, while layer 7 (L7) protocol detail exists only where an L7 network policy sends the traffic through Cilium's proxy to be parsed.

## Where the instrumentation lives

Cilium, when used as the cluster's Container Network Interface (CNI) plugin, attaches eBPF programs at the socket and at the veth/network device for every pod. Those programs are the enforcement path: they already decide, per packet, whether traffic is forwarded or denied. **Hubble adds no new interception point; it consumes the events those programs emit.** This is the structural difference from a service mesh, where a userspace proxy is inserted into the data path and each observed byte is copied through it.

The consequence for identity is more important than the consequence for overhead. Cilium does not identify workloads by IP address but by a **security identity** derived from labels, and the datapath carries that identity with the packet. A flow record therefore names `namespace/pod` on both ends at the moment the verdict is taken, so a drop is attributable without cross-referencing an IP against a pod list that has since changed.

## The three components

- **Hubble API, per node.** The Cilium agent on each node exposes that node's flows over a Unix domain socket. The scope is **strictly on-node**: an agent knows only about traffic its own datapath observed.
- **Hubble Relay, cluster-wide.** Relay connects to every agent and fans their streams into a single endpoint, covering one cluster or several joined in a ClusterMesh. A cluster-wide query is therefore a fan-out over agents, not a read from a central store.
- **Hubble UI.** A web application that discovers the service dependency graph at L3/L4 and L7 and renders it as a filterable service map.

Metrics are a separate export path. Agents can publish Hubble metrics to Prometheus — flows by verdict, HTTP request rates and latencies, DNS, TCP flags — **without raw flows being stored at all**, which is the mode that survives long retention windows.

## Enabling and the L7 boundary

```bash
cilium hubble enable --ui        # enables Hubble + Relay + UI
cilium status                    # confirm Hubble: OK
cilium hubble port-forward &     # expose Relay locally on :4245
hubble status                    # flows/s, connected nodes
```

L7 visibility — HTTP paths and methods, gRPC, DNS names, Kafka — is **not implicit**. It is produced by an L7 network policy that selects the pods whose traffic is to be parsed, which routes that traffic through Cilium's proxy. Absent such a policy, `--protocol http` matches nothing even while the underlying TCP connections are being recorded normally, because the L3/L4 record contains no HTTP fields to match on.

## Reading flows

`hubble observe` streams the flow log and applies its filters **server-side**, so selection happens before records cross the wire rather than in a downstream `grep`.

```bash
# Live HTTP requests hitting one pod
hubble observe --pod deathstar --protocol http --follow

# Only drops in a namespace — locates a policy that is denying traffic
hubble observe --namespace prod --verdict DROPPED

# One service to another, JSON for piping into jq
hubble observe --from-pod prod/api --to-pod prod/db -o json
```

A forwarded L7 flow:

```text
May 4 13:23:40.501: default/tiefighter:42690 -> default/deathstar-c74d84667-cx5kp:80
  http-request FORWARDED (HTTP/1.1 POST http://deathstar.default.svc.cluster.local/v1/request-landing)
```

A denied one has the same shape and a different verdict:

```text
May 4 13:23:43.791: default/tiefighter:42742 -> default/deathstar-c74d84667-cx5kp:80
  http-request DROPPED (HTTP/1.1 PUT http://deathstar.default.svc.cluster.local/v1/exhaust-port)
```

Each record carries source and destination identity, port, the L7 verb and path where parsing is enabled, and the `FORWARDED`/`DROPPED` verdict. **The verdict and the identities originate from the same evaluation**, which is what makes the second line a direct statement about policy rather than an inference from a missing reply.

| Question | Command |
|---|---|
| Is a new NetworkPolicy dropping legitimate traffic? | `hubble observe --verdict DROPPED --namespace prod` |
| What does service X talk to? | `hubble observe --from-pod prod/x`, then read the map in the UI |
| Which HTTP routes return 500? | enable L7, then `hubble observe --protocol http --http-status 500` |
| Are DNS lookups failing? | `hubble observe --protocol dns` |

## Scope of the answer

Hubble answers connectivity and policy questions: which identity was denied, whether A reaches B at all, what the topology is. It is **flow-level, not request-causal** — a flow record describes one hop as one datapath saw it and carries no propagated trace context, so reconstructing a request across several services remains the job of distributed tracing such as OpenTelemetry. The two coexist: Hubble's L7 metrics land in the same Prometheus and Grafana stack that already receives trace-derived signals.

A safe first exercise is to enable Hubble, apply a deny-all NetworkPolicy to a single test namespace, and run `hubble observe --namespace <ns> --verdict DROPPED --follow` to enumerate the identities that policy blocks before widening its scope.

## Pitfalls

- **`--protocol http` returns nothing while the service is plainly serving traffic.** L7 parsing was never enabled for those pods; an L7 network policy selecting them is what turns it on.
- **A query run against the on-node API silently omits most of the cluster.** The per-node Hubble API is limited to flows that node's datapath observed; cluster-wide answers require Relay.
- **Flows disappear after a node or agent restart.** Flow records are held by the agent, not in a durable store; retaining history over long windows is the job of the Prometheus metrics export, which aggregates rather than preserving individual flows.
- **A drop that never appears in the log.** Traffic denied before it reaches the Cilium datapath — outside the eBPF programs' attachment points — produces no Hubble verdict, so absence of a `DROPPED` record is not evidence that policy allowed the connection.
- **Filtering client-side on large clusters.** Piping an unfiltered `hubble observe` into `grep` moves every record through Relay; the `--namespace`, `--pod` and `--verdict` filters are evaluated server-side and cut the stream at the source.
