---
title: "Istio ambient mesh: the sidecarless service mesh, GA since v1.24"
date: 2026-07-26
track: microservices
summary: "Istio's ambient mode splits the sidecar into two shared proxies — ztunnel for L4 mTLS per node, waypoint for L7 policy per namespace — removing the per-pod Envoy tax. GA landed in Istio 1.24 (November 2024); current stable is 1.30."
reading_time: 5
tags: [istio, service-mesh, ambient-mesh, ztunnel, envoy, kubernetes, mtls]
sources:
  - title: "Istio — Fast, Secure, and Simple: Istio's Ambient Mode Reaches General Availability in v1.24"
    url: "https://istio.io/latest/blog/2024/ambient-reaches-ga/"
  - title: "Istio — Ambient overview (docs)"
    url: "https://istio.io/latest/docs/ambient/overview/"
  - title: "Istio — Enable ambient mode (docs)"
    url: "https://istio.io/latest/docs/ambient/migrate/enable-ambient-mode/"
  - title: "Solo.io — Do more for less with Istio Ambient mode"
    url: "https://www.solo.io/blog/istio-more-for-less"
  - title: "CNCF — Ambient mesh: can sidecar-less Istio make your application faster?"
    url: "https://www.cncf.io/blog/2024/08/23/ambient-mesh-can-sidecar-less-istio-make-your-application-faster/"
---

Saltmere already covered Burns' generic **sidecar pattern** — a second container riding shotgun in your pod, sharing its network namespace. Istio's classic mesh is the canonical example: inject an Envoy sidecar into every workload pod and it silently intercepts every packet in and out. Ambient mode is Istio saying that pattern doesn't scale past a certain fleet size, and ripping the sidecar out entirely. This is that story specifically — not sidecars in general, but Istio removing them.

## The sidecar tax

Classic Istio (`istio-injection=enabled`) mutates every pod to add an Envoy container plus an `istio-init` container that rewrites `iptables` rules. Each Envoy carries its own connection pools, xDS config cache, and CPU/memory reservation — multiplied by pod count, not by node or namespace count. A 200-pod namespace means 200 Envoys, each independently idling, each independently restarting when the app pod restarts, each adding a network hop even for traffic that only needs plain mTLS. Upgrading Istio means a rolling restart of every application pod to reinject the sidecar. It works, but the resource and operational bill scales linearly with your busiest number: total pods.

## Ambient's two-layer split

Ambient mode (GA'd as a per-namespace opt-in, coexisting with sidecar mode in the same mesh) replaces the one-sidecar-per-pod model with two purpose-built, shared proxies:

- **ztunnel** (zero-trust tunnel) — a lightweight, Rust-based proxy that runs **once per node** as a DaemonSet. It handles L3/L4 only: mutual TLS, peer authentication, L4 authorization, and telemetry. It never parses HTTP. Traffic between pods on different nodes is tunneled over HBONE (HTTP CONNECT-based overlay network encapsulation), so ztunnel-to-ztunnel hops carry mTLS without touching application bytes.
- **waypoint proxy** — a full Envoy instance, but deployed **per namespace or per service account**, not per pod. It only turns on when a workload actually needs L7 features: HTTP routing, retries, rich `AuthorizationPolicy` rules, fault injection. A namespace that only needs mTLS and L4 authorization never gets a waypoint at all.

The effect: instead of N Envoys for N pods, you get one ztunnel per node and, optionally, one waypoint per namespace that needs L7. Application pods carry zero mesh containers — traffic redirection happens via a CNI plugin (or ztunnel-managed routing) rather than an injected sidecar, so pod restarts and upgrades no longer touch mesh version.

## Enabling it

Ambient is opt-in per namespace via a label — no pod annotation, no restart-to-inject:

```bash
# install Istio with the ambient profile (installs istiod, ztunnel, CNI)
istioctl install --set profile=ambient --skip-confirmation

# enroll a namespace into the ambient data plane — L4 only, via ztunnel
kubectl label namespace checkout istio.io/dataplane-mode=ambient

# check that ztunnel picked up the workloads
kubectl get pods -n istio-system -l app=ztunnel -o wide
```

L7 features are added on top, scoped to a namespace or a specific service, by deploying a waypoint:

```bash
# deploy a waypoint for the checkout namespace's default service account,
# then have istiod start routing that traffic through it for L7 policy
istioctl waypoint apply -n checkout --enroll-namespace
```

```yaml
# example: an L7 AuthorizationPolicy that only takes effect once a waypoint
# is handling the namespace (ztunnel alone can't evaluate HTTP paths)
apiVersion: security.istio.io/v1
kind: AuthorizationPolicy
metadata:
  name: checkout-read-only
  namespace: checkout
spec:
  targetRefs:
    - kind: Service
      group: ""
      name: checkout-svc
  action: DENY
  rules:
    - to:
        - operation:
            methods: ["POST", "PUT", "DELETE"]
```

That AuthorizationPolicy is inert under ztunnel alone (L4 can't see HTTP methods) — it only enforces once traffic is routed through a waypoint, which is the point of the layered design: pay for L7 only where you use it.

## Sidecar vs ambient

| | Sidecar mode | Ambient mode |
|---|---|---|
| Proxy placement | One Envoy injected per pod | ztunnel per node + optional waypoint per namespace |
| mTLS/L4 handled by | Per-pod Envoy | Shared ztunnel (Rust, no HTTP parsing) |
| L7 routing/policy | Always-on Envoy per pod | Opt-in waypoint, only where needed |
| Pod restarts on mesh upgrade | Yes — sidecar reinjection | No — ztunnel/waypoint upgrade independently |
| Reported CPU overhead | Baseline (100%) | ~73% less CPU at L4 vs. sidecar mTLS (Solo.io benchmark) |
| Mesh membership | Binary per pod | Mixed sidecar + ambient workloads in one mesh |
| Maturity | Stable since Istio 1.1 (2019) | GA in Istio 1.24, November 7 2024 |

## What "GA" actually means here, and the honest caveats

Ambient mode was introduced as a concept in 2022, reached beta in Istio 1.22 (v1.22, mid-2024), and Istio's official blog post "Fast, Secure, and Simple: Istio's Ambient Mode Reaches General Availability in v1.24" confirms GA landed with **Istio 1.24, published November 7, 2024**. Current stable at time of writing is the **1.30.x** line. That's several minor releases of production hardening past GA — worth checking the specific patch release notes if you're planning an ambient rollout today, since waypoint and multicluster ambient support kept iterating well past the GA milestone (ambient multicluster support, for example, is a 2026-era addition on top of the original GA baseline).

The efficiency numbers are real but workload-dependent: Solo.io's benchmark found ambient's ztunnel-only path used roughly 73% less CPU than sidecar Envoy for equivalent mTLS traffic, and one adopter cited in Istio's GA post cut running containers by about 45% after removing sidecars. Those gains shrink once you add waypoints back for L7-heavy services — a waypoint is still a full Envoy, so a namespace where every service needs rich routing and retries won't see the same delta as one that only needed mTLS. Competing mesh vendors (Linkerd/Buoyant) have argued the two-hop ztunnel-to-waypoint L7 path can add latency versus a single co-located sidecar for chatty, policy-heavy traffic — a fair counterpoint worth benchmarking against your own traffic shape rather than taking either vendor's number at face value.

## When to reach for it

Ambient is the right call when most of your mesh traffic only needs mTLS and coarse L4 authorization — the common case for a lot of internal service-to-service calls — and you want that without a per-pod tax or coupling mesh upgrades to app pod restarts. Keep sidecars (or add waypoints selectively) where you're leaning on L7 traffic shaping, header-based routing, or complex `AuthorizationPolicy` rules today, and migrate namespace by namespace since ambient and sidecar workloads coexist in the same mesh.

**Try next:** Spin up a `kind` cluster, install Istio with `--set profile=ambient`, label two namespaces `istio.io/dataplane-mode=ambient`, and run `istioctl proxy-status` alongside `kubectl top pods -n istio-system` before and after adding a waypoint to just one of them — compare the CPU delta against what Solo.io reported for your own workload shape.
