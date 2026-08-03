---
title: "The Gateway API: Ingress grew up and split into three roles"
date: 2026-08-03
track: microservices
summary: "Ingress crammed routing, TLS, and vendor knobs into annotations owned by nobody. The Kubernetes Gateway API replaces it with three role-oriented resources — GatewayClass, Gateway, HTTPRoute — that split infra from app concerns. Current as of v1.6.1 (July 2026), with GAMMA for east-west mesh and a 2025 Inference Extension for LLM routing."
reading_time: 6
tags: [kubernetes, gateway-api, ingress, networking, service-mesh, gamma]
sources:
  - title: "Gateway API — official docs (gateway-api.sigs.k8s.io)"
    url: "https://gateway-api.sigs.k8s.io/"
  - title: "Gateway API v1.5: Moving features to Stable — Kubernetes blog"
    url: "https://kubernetes.io/blog/2026/04/21/gateway-api-v1-5/"
  - title: "Gateway API v1.6.0 release notes — GitHub"
    url: "https://github.com/kubernetes-sigs/gateway-api/releases/tag/v1.6.0"
  - title: "Gateway API for Service Mesh (GAMMA) — official docs"
    url: "https://gateway-api.sigs.k8s.io/mesh/"
  - title: "Introducing Gateway API Inference Extension — Kubernetes blog"
    url: "https://kubernetes.io/blog/2025/06/05/introducing-gateway-api-inference-extension/"
---

Ingress had one resource and a fatal flaw: everything that mattered lived in annotations. Path rewrites, canary weights, TLS policy, timeout tuning — all of it went into `nginx.ingress.kubernetes.io/*` or `alb.ingress.kubernetes.io/*` strings that were vendor-specific, unvalidated, and owned by whoever happened to `kubectl apply` last. There was no seam between "the platform team runs the load balancer" and "the app team owns their routes." One `Ingress` object mixed both, so both teams edited the same YAML and hoped.

The Kubernetes **Gateway API** is the official successor, and its whole design premise is that north-south traffic (client-to-cluster) has *three* audiences, not one. It splits Ingress into three resources with three owners.

## GatewayClass, Gateway, HTTPRoute — one resource per role

- **GatewayClass** — installed by the *infrastructure provider* (the controller/vendor: Istio, Envoy Gateway, Cilium, NGINX, a cloud LB). It's the cluster-scoped template that says "Gateways of this class are backed by *this* implementation." Think `StorageClass`, but for load balancers.
- **Gateway** — created by the *cluster operator / platform team*. It requests an actual data-plane instance: "give me a listener on 443 with this TLS cert, in this namespace." This is the infra concern — addresses, ports, certificates.
- **HTTPRoute** — created by the *application developer*. It attaches to a Gateway via `parentRefs` and describes matching and forwarding rules: hostnames, path prefixes, header matches, weighted backends, filters. This is the app concern, and it can live in the app's own namespace.

The seam is `parentRefs` (route → gateway) plus `ReferenceGrant` for cross-namespace permission. A platform team runs one hardened Gateway; dozens of app teams attach HTTPRoutes to it without ever touching the listener config or fighting over annotations. Everything is a typed, versioned CRD field, so `kubectl` and admission control actually validate it. There are sibling routes for other protocols — `GRPCRoute`, `TLSRoute`, `TCPRoute`, `UDPRoute` — following the same attachment model.

## A real Gateway + HTTPRoute

Platform team owns the Gateway (namespace `infra`), app team owns the route (namespace `shop`):

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: prod-gateway
  namespace: infra
spec:
  gatewayClassName: envoy          # points at an installed GatewayClass
  listeners:
    - name: https
      protocol: HTTPS
      port: 443
      hostname: "*.example.com"
      tls:
        mode: Terminate
        certificateRefs:
          - kind: Secret
            name: example-com-tls
      allowedRoutes:
        namespaces:
          from: Selector           # only namespaces the operator permits
          selector:
            matchLabels: { gateway-access: "true" }
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: checkout
  namespace: shop
spec:
  parentRefs:
    - name: prod-gateway
      namespace: infra
  hostnames:
    - "shop.example.com"
  rules:
    - matches:
        - path: { type: PathPrefix, value: /checkout }
      filters:
        - type: RequestHeaderModifier
          requestHeaderModifier:
            add: [{ name: X-Env, value: prod }]
      backendRefs:
        - name: checkout-v2
          port: 8080
          weight: 90
        - name: checkout-v1
          port: 8080
          weight: 10          # 90/10 canary, no annotations required
```

The canary split, header injection, and TLS all sit in first-class fields. `allowedRoutes` is the guardrail that lets the operator decide *which* namespaces may bind to this Gateway — the governance that Ingress never had.

## Where the spec is right now (mid-2026)

The core resources — GatewayClass, Gateway, HTTPRoute — have been GA (`v1`) since v1.0 in late 2023. Since then the project has shipped on a roughly quarterly cadence, moving features up the **Standard** (GA) vs **Experimental** channel ladder.

The **latest release is v1.6.1, published July 16, 2026** (a patch on top of **v1.6.0, June 29, 2026**). v1.6.0's headline change was graduating **TCPRoute and UDPRoute to GA** at `v1`. The prior minor, **v1.5** (v1.5.0 on February 27, 2026; announced on the Kubernetes blog as "Moving features to Stable"), promoted six long-requested features to the Standard channel: **ListenerSet, TLSRoute, the HTTPRoute CORS filter, client certificate validation, certificate selection for Gateway TLS, and origination ReferenceGrant**. (Note the announcement blog post is dated later than the tag — cite the tag date for the release itself.) A large conformance program lets implementations declare exactly which features they support, so "does my controller do X" has an answerable, tested answer rather than a vendor's word.

## GAMMA: the same API for east-west mesh traffic

Gateway API started as north-south (edge ingress), but the **GAMMA** initiative — Gateway API for Mesh Management and Administration — extends the *same* HTTPRoute to east-west, service-to-service traffic inside a mesh. The trick is elegant: instead of a route's `parentRefs` pointing at a Gateway, it points at a **Service**. That reparents the route from "the edge" to "everyone who calls this service," and the mesh data plane (Istio, Linkerd, Cilium, Kuma) enforces it. So a service owner writes one kind of HTTPRoute whether they're shaping ingress or in-mesh traffic. This is the throughline connecting Gateway API to sidecarless meshes like Istio ambient — the routing vocabulary is shared, only the enforcement point differs.

## The 2025 trend: the Inference Extension

The genuinely new direction is the **Gateway API Inference Extension**, introduced on the Kubernetes blog in June 2025. It turns a Gateway-API-compatible proxy into an **inference gateway** for self-hosted generative models, using Envoy's external processing (`ext_proc`) plus new CRDs (an `InferencePool` of model-serving endpoints) to do routing that a normal L7 balancer can't: **KV-cache-aware and request-cost-aware scheduling, LoRA-adapter routing, and model-level traffic splitting**. It reached GA and now ships productized as GKE Inference Gateway and via Istio's inference support. If you're serving LLMs on Kubernetes, this is where round-robin stops being good enough and the Gateway API gives you a standard place to plug in smarter scheduling.

The practical takeaway: if you're still on Ingress, the migration path is real and the roles finally match your org chart. Start with a single Gateway owned by the platform team, and let app teams bring their own HTTPRoutes.

**Try next:** Install a conformant controller (Envoy Gateway or Istio), apply the Gateway + HTTPRoute above, then port one real Ingress — move its host/path rules into an HTTPRoute and its TLS into the Gateway listener — and check `kubectl get httproute -o wide` for the `Accepted`/`ResolvedRefs` status conditions.
