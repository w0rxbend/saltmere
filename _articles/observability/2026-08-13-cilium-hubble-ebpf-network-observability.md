---
title: "Hubble: eBPF network flow visibility for Kubernetes, no sidecars"
date: 2026-08-13
track: observability
summary: "Cilium's Hubble taps the same eBPF programs already enforcing your network policy to emit L3/L4/L7 flow logs, a live service dependency map, and Prometheus metrics — no proxies injected into pods, no app changes. Here's how it works and how to read flows from the CLI."
reading_time: 5
tags: [cilium, hubble, ebpf, kubernetes, network-observability]
sources:
  - title: "Network Observability with Hubble — Cilium 1.20 docs"
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

If Cilium (currently stable **1.20**) is your CNI, you already have the hard part of network observability installed. Cilium's datapath runs eBPF programs at the kernel — at the socket and at the veth/network device — to route, load-balance, and enforce policy for every pod. Hubble is the observability layer that reads events off those same programs. Because the instrumentation is in the kernel, not in the app, you get visibility into *every* connection with no sidecar proxy, no library, and no code change — and near-zero per-packet overhead compared to a userspace proxy that copies traffic.

## The three pieces

- **Hubble API (per node).** The Cilium agent on each node exposes flows for that node over a Unix socket. This is always on-node scope.
- **Hubble Relay (cluster-wide).** Deploy Relay and it fans out to every agent, giving you one endpoint for the whole cluster — or multiple clusters in a ClusterMesh.
- **Hubble UI.** A web app that auto-discovers the service dependency graph at L3/L4 and L7 and renders it as a filterable service map.

Metrics are separate: agents can export Hubble metrics to Prometheus (flows by verdict, HTTP request rates and latencies, DNS, TCP flags) without you storing raw flows at all.

## Turn it on

```bash
cilium hubble enable --ui        # enables Hubble + Relay + UI
cilium status                    # confirm Hubble: OK
cilium hubble port-forward &     # expose Relay locally on :4245
hubble status                    # flows/s, connected nodes
```

L7 visibility (HTTP paths, methods, gRPC, DNS names, Kafka) isn't automatic — it comes from a Cilium *visibility annotation* or an L7 network policy that tells the datapath to parse those protocols for the selected pods. L3/L4 flows are free the moment Hubble is on.

## Reading flows

`hubble observe` is the workhorse. It streams the flow log with rich server-side filters, so you're not grepping.

```bash
# Live HTTP requests hitting one pod
hubble observe --pod deathstar --protocol http --follow

# Only drops in a namespace — fastest way to find a policy that's biting
hubble observe --namespace prod --verdict DROPPED

# One service to another, JSON for piping into jq
hubble observe --from-pod prod/api --to-pod prod/db -o json
```

A forwarded L7 flow reads like this:

```text
May 4 13:23:40.501: default/tiefighter:42690 -> default/deathstar-c74d84667-cx5kp:80
  http-request FORWARDED (HTTP/1.1 POST http://deathstar.default.svc.cluster.local/v1/request-landing)
```

and a policy drop is unmistakable — same shape, different verdict:

```text
May 4 13:23:43.791: default/tiefighter:42742 -> default/deathstar-c74d84667-cx5kp:80
  http-request DROPPED (HTTP/1.1 PUT http://deathstar.default.svc.cluster.local/v1/exhaust-port)
```

Each line carries source and destination identity (namespace/pod, not just IP — Cilium tracks security identities), port, the L7 verb and path when parsed, and the `FORWARDED`/`DROPPED` verdict. That identity-awareness is why the drop above instantly names *who* was denied *what*, without you cross-referencing IPs against a churning pod list.

| Question | Command |
|---|---|
| Is my new NetworkPolicy dropping legit traffic? | `hubble observe --verdict DROPPED --namespace prod` |
| What does service X actually talk to? | `hubble observe --from-pod prod/x` then read the map in the UI |
| Which HTTP routes 500? | enable L7, `hubble observe --protocol http --http-status 500` |
| DNS lookups failing? | `hubble observe --protocol dns` |

## When to reach for it

Hubble answers connectivity and policy questions in seconds — "who is being dropped," "does A even reach B," "what's the service topology" — the exact questions a sidecar mesh answers but without the mesh's proxy tax. It's flow-level, not distributed tracing: for request causality across services you still want OpenTelemetry traces. Run both; they're complementary, and Hubble's L7 metrics feed the same Prometheus/Grafana stack your traces already use.

**Try next:** `cilium hubble enable --ui`, apply a deny-all NetworkPolicy to one test namespace, then `hubble observe --namespace <ns> --verdict DROPPED --follow` and watch exactly which identities the policy blocks before you roll it wider.
