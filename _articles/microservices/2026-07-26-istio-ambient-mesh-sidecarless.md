---
title: "Istio ambient mesh: the sidecarless data plane, GA since v1.24"
date: 2026-07-26
track: microservices
summary: "Istio's ambient mode splits the sidecar into two shared proxies — ztunnel for L4 mTLS per node, waypoint for L7 policy per namespace — removing the per-pod Envoy. GA landed in Istio 1.24 (November 2024); several minor releases have shipped since."
reading_time: 6
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

**Gist.** A classic Istio mesh places one Envoy proxy inside every workload pod, so proxy count, memory reservation and upgrade blast radius all scale with total pod count. Ambient mode replaces that with two shared proxies — a per-node **ztunnel** handling layer 4 (L4) mutual TLS (mTLS) and a per-namespace **waypoint** handling layer 7 (L7) policy — so the L7 proxy is provisioned only where L7 features are used. The cost is an extra network hop: L7 traffic traverses ztunnel and then a waypoint that is no longer co-located with the workload.

## The per-pod proxy cost

Classic Istio, enabled by labelling a namespace `istio-injection=enabled`, mutates every pod at admission time to add an Envoy container plus an `istio-init` container that rewrites `iptables` rules inside the pod's network namespace. Each Envoy holds **its own connection pools, its own xDS configuration cache, and its own CPU and memory requests**. Those are multiplied by pod count, not by node or namespace count: a 200-pod namespace runs 200 Envoys, each idling independently, and each interposed on traffic that may need nothing beyond mTLS.

Two operational consequences follow from the injection mechanism rather than from Envoy itself. First, **the mesh version is bound to the pod lifecycle**: upgrading Istio requires a rolling restart of every application pod so the mutating webhook can reinject the newer sidecar. Second, mesh membership is **binary per pod** — a pod is either injected or it is not, decided at creation.

## The two-layer split

Ambient mode is a per-namespace opt-in that coexists with sidecar mode inside the same mesh. It decomposes the sidecar's responsibilities along the L4/L7 boundary:

- **ztunnel** (zero-trust tunnel) is a Rust proxy deployed **once per node** as a DaemonSet. Its scope is L3/L4 only: mutual TLS, peer authentication, L4 authorization and telemetry. **It does not parse HTTP.** Traffic between pods on different nodes is carried over **HBONE** (HTTP-Based Overlay Network Environment), a tunnel built on HTTP CONNECT, so the ztunnel-to-ztunnel hop transports mTLS-protected bytes without the proxy interpreting the application protocol.
- **waypoint** is a full Envoy instance deployed **per namespace or per service account**, not per pod. It is provisioned only when L7 behaviour is required: HTTP routing, retries, `AuthorizationPolicy` rules that match on HTTP attributes, fault injection. A namespace needing only mTLS and L4 authorization has **no waypoint at all**.

The resulting shape is one ztunnel per node plus, optionally, one waypoint per namespace that needs L7, in place of N Envoys for N pods. **Application pods carry no mesh container.** Redirection into the data plane is performed outside the pod — by the Istio CNI (Container Network Interface) plugin and ztunnel-managed routing — rather than by an injected `istio-init` container, which is why enrolment and mesh upgrades no longer require restarting application pods.

## The invariant that governs policy

The load-bearing rule is that **a policy is enforced only by a proxy that can observe the attribute it matches on**. ztunnel observes identities, ports and connection metadata; it does not observe HTTP methods, paths or headers. An `AuthorizationPolicy` written against HTTP attributes is therefore **inert in a namespace with no waypoint** — it is accepted by the API server, it appears in `kubectl get authorizationpolicy`, and it denies nothing. The failure mode is silent and fails open: a rule intended to block writes admits them, with no error surfaced at apply time.

## Enrolment

Enrolment is a namespace label. There is no pod annotation and no restart-to-inject step:

```bash
# install Istio with the ambient profile (istiod, ztunnel DaemonSet, Istio CNI)
istioctl install --set profile=ambient --skip-confirmation

# enrol a namespace into the ambient data plane — L4 only, served by ztunnel
kubectl label namespace checkout istio.io/dataplane-mode=ambient

# confirm the ztunnel DaemonSet has a pod on each node hosting the workloads
kubectl get pods -n istio-system -l app=ztunnel -o wide
```

L7 capability is added afterwards, scoped to a namespace or a single service:

```bash
# deploy a waypoint for the checkout namespace and route its traffic through it
istioctl waypoint apply -n checkout --enroll-namespace

# verify the waypoint exists and which workloads are bound to it
kubectl get gateway -n checkout
istioctl proxy-status
```

The policy below matches on HTTP methods, so it takes effect only once the waypoint above is handling the namespace:

```yaml
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

## Sidecar and ambient compared

| | Sidecar mode | Ambient mode |
|---|---|---|
| Proxy placement | One Envoy injected per pod | ztunnel per node + optional waypoint per namespace |
| mTLS/L4 handled by | Per-pod Envoy | Shared ztunnel (Rust, no HTTP parsing) |
| L7 routing/policy | Always-on Envoy per pod | Opt-in waypoint, only where deployed |
| Pod restarts on mesh upgrade | Yes — sidecar reinjection | No — ztunnel and waypoint upgrade independently |
| Reported CPU overhead | Baseline | Substantially lower at L4 than sidecar mTLS (Solo.io benchmark) |
| Mesh membership | Binary per pod | Sidecar and ambient workloads coexist in one mesh |
| Maturity | Stable since Istio 1.0 (2018) | GA in Istio 1.24, November 2024 |

## What GA covers, and what the numbers support

Ambient mode was introduced as a concept in 2022, reached beta in Istio 1.22 in mid-2024, and reached general availability in **Istio 1.24, published in November 2024**, per Istio's blog post "Fast, Secure, and Simple: Istio's Ambient Mode Reaches General Availability in v1.24". Several minor releases have shipped past the GA baseline since. Functionality continued to be added after that milestone — ambient multicluster support, for instance, arrived later — so the GA label attaches to the 1.24 feature set rather than to everything now shipped under the ambient name.

The efficiency figures are workload-dependent. Solo.io's benchmark measured the ztunnel-only path using **substantially less CPU than sidecar Envoy for equivalent mTLS traffic**, and Istio's GA post cites adopters reporting **a large drop in running container count** after removing sidecars. Both figures describe the L4-only case. **A waypoint is a full Envoy**, so a namespace where every service requires rich routing and retries reintroduces Envoy cost, and the container-count reduction shrinks accordingly. Competing mesh vendors (Linkerd/Buoyant) have argued that the two-hop ztunnel-to-waypoint L7 path adds latency relative to a single co-located sidecar for chatty, policy-heavy traffic; no neutral published benchmark settles that comparison, which makes it a measurement to take against a specific traffic shape rather than a claim to accept from either side.

The migration path follows from coexistence: namespaces can be converted one at a time, with sidecars retained where L7 traffic shaping, header-based routing or HTTP-attribute `AuthorizationPolicy` rules are already load-bearing.

## Pitfalls

- **An HTTP-matching `AuthorizationPolicy` in a waypointless ambient namespace is accepted and silently ignored.** ztunnel cannot read HTTP methods or paths, so a `DENY` rule on `POST` blocks nothing until a waypoint handles the traffic.
- **Labelling the namespace does not move traffic that bypasses the CNI redirection.** Ambient enrolment depends on the Istio CNI plugin being installed and functioning on each node; a node whose CNI chain is misconfigured carries unenrolled traffic while the namespace label suggests otherwise.
- **A ztunnel is per node, so its failure domain is the node, not the pod.** Every ambient workload scheduled on that node shares the same proxy process, unlike a sidecar whose blast radius is one pod.
- **Adding a waypoint reintroduces Envoy resource cost.** Benchmarked savings quoted for ambient describe the ztunnel-only L4 path; a namespace waypointed for L7 is not covered by those figures.
- **The published efficiency figures come from vendor benchmarks.** The CPU reduction was reported by Solo.io and the container-count reduction by an adopter quoted in Istio's own GA post; neither is an independent measurement of an arbitrary workload.
