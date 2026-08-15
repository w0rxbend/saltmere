---
title: "The Gateway API: Ingress split into three role-owned resources"
date: 2026-08-03
track: microservices
summary: "Ingress crammed routing, TLS, and vendor knobs into annotations owned by nobody. The Kubernetes Gateway API replaces it with three role-oriented resources — GatewayClass, Gateway, HTTPRoute — that separate infrastructure concerns from application concerns. Current as of v1.6.0, with GAMMA for east-west mesh traffic and the Inference Extension for model serving."
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

**Gist.** The Kubernetes `Ingress` resource expressed everything beyond host-and-path matching as vendor-prefixed annotations — free-form strings that the API server does not validate and that no single team owns. The Gateway API replaces the single resource with three typed custom resources, **GatewayClass, Gateway and HTTPRoute**, each intended for a different owner, joined by explicit reference fields (`parentRefs`, `ReferenceGrant`) rather than by co-editing one object. The cost is a larger surface: several custom resource definitions (CRDs), a controller that must be installed separately, a two-channel release process whose feature set varies by implementation, and a cross-namespace permission model that has to be configured before routes bind.

## Why annotations failed as an extension point

Under `Ingress`, path rewriting, canary weights, timeout tuning and TLS policy were carried in keys such as `nginx.ingress.kubernetes.io/*` or `alb.ingress.kubernetes.io/*`. Three properties follow from that encoding. The keys are **vendor-specific**, so the object is not portable between controllers. They are **unvalidated**: annotation values are opaque strings to the API server, so a typo is accepted at admission and fails — or silently does nothing — in the data plane. And they are **co-located with the routing rules**, so the team that operates the load balancer and the team that owns the application edit the same object. `Ingress` offers no field that expresses "this listener exists, and these namespaces may attach to it".

## One resource per role

- **GatewayClass** — cluster-scoped, installed by the infrastructure provider (the controller implementation: Istio, Envoy Gateway, Cilium, NGINX, a cloud load balancer). It declares that Gateways naming this class are reconciled by that implementation. The structural analogue is `StorageClass`.
- **Gateway** — created by the cluster operator. It requests a data-plane instance and describes the infrastructure-facing properties: **addresses, listeners, ports, protocols, TLS certificates**, and — through `allowedRoutes` — which namespaces may attach routes to each listener.
- **HTTPRoute** — created by the application team, in the application's own namespace. It carries matching and forwarding rules: hostnames, path and header matches, filters, and weighted `backendRefs`.

The join between the last two is `parentRefs` on the route, naming a Gateway. Because the route may live in a different namespace from the Gateway, and its backends may live elsewhere again, cross-namespace references are gated by **`ReferenceGrant`**, a resource created in the *target* namespace that names the permitted source kind and namespace. **The default is denial**: without an `allowedRoutes` selector that admits the route's namespace, and without a `ReferenceGrant` for cross-namespace backend or secret references, the reference is not resolved.

Every field is part of a versioned CRD schema, so the API server validates structure at admission rather than deferring the error to the controller. Sibling route kinds — `GRPCRoute`, `TLSRoute`, `TCPRoute`, `UDPRoute` — use the same attachment model.

## Status conditions are the observable contract

Attachment is not a synchronous operation. A route is written, a controller reconciles it, and the result appears in `status`, per parent reference. Two conditions carry the outcome: **`Accepted`**, meaning the parent Gateway admitted the route (namespace permitted by `allowedRoutes`, hostname intersecting the listener's), and **`ResolvedRefs`**, meaning every `backendRef` and secret reference named by the route resolved to an existing, permitted object. A route can be `Accepted: True` and `ResolvedRefs: False` — admitted at the listener, but forwarding to a backend that does not exist or is not granted. This is the failure mode that most resembles the old annotation problem, except that it is now reported in a field rather than inferred from traffic.

## A Gateway and an attached route

The platform team owns the Gateway in namespace `infra`; the application team owns the route in namespace `shop`.

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: prod-gateway
  namespace: infra
spec:
  gatewayClassName: envoy          # names an installed GatewayClass
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
          from: Selector           # not `All` — attachment is opt-in
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
    - "shop.example.com"           # must intersect the listener hostname
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
          weight: 10               # 90/10 split as typed fields
```

Two properties of this listing are load-bearing. The traffic split, the header injection and the TLS termination are **typed fields subject to schema validation**, not annotation strings. And the namespace `shop` binds to the listener only because it carries the label `gateway-access: "true"`; the selector is the operator's admission control over which teams may attach.

## Release state as of mid-2026

The three core resources reached general availability at `v1` in Gateway API v1.0, released in late 2023. Features advance through two channels, **Experimental** and **Standard**. As of v1.5 the project ships on a release-train model: whatever has reached feature freeze — and has its documentation complete — goes out together in the next release.

The **v1.6.0** release graduated **TCPRoute and UDPRoute to `v1`**, tightened HTTPRoute retry validation (retry codes must be unique, attempts at least 1) and raised the ceiling on certificate-authority references from 8 to 16. The preceding minor, **v1.5.0 of 27 February 2026**, promoted six features to the Standard channel: **ListenerSet** (listeners defined separately and merged onto a target Gateway, which lifts the limit of 64 listeners per Gateway), **TLSRoute**, the **HTTPRoute cross-origin resource sharing (CORS) filter**, **client certificate validation**, **certificate selection for Gateway TLS origination**, and **`ReferenceGrant`**. A conformance program lets an implementation declare which features it supports, so support for a given feature is a tested claim rather than a vendor assertion.

## GAMMA: the same route kind for east-west traffic

The **GAMMA** initiative — Gateway API for Mesh Management and Administration — applies the same `HTTPRoute` kind to service-to-service traffic inside a mesh. The mechanism is a change of parent: rather than `parentRefs` naming a Gateway, it names a **Service**. The route then governs traffic addressed to that Service by any in-mesh client, and the mesh data plane (Istio, Linkerd, Cilium, Kuma) enforces it. The route vocabulary is identical for ingress and in-mesh traffic; the enforcement point differs.

## The Inference Extension

The **Gateway API Inference Extension** was introduced on the Kubernetes blog in June 2025. It makes a Gateway API-conformant proxy act as an inference gateway for self-hosted generative models, using an external processing callout together with two new CRDs: **`InferencePool`**, a platform-owned pool of model-serving pods on shared accelerator compute, and **`InferenceModel`**, a workload-owned mapping from a public model name to the models served by a pool, including traffic splitting between them. The routing decisions its Endpoint Selection Extension adds are ones a general layer-7 balancer does not make: **model-aware endpoint selection, per-request criticality** (an interactive chat request outranking a batch job), and **load balancing on real-time model-server metrics** rather than connection counts. The blog introducing it describes the project as alpha; the status at any later date is a question for the project's own release notes.

## Pitfalls

- **A route is `Accepted: False` with no traffic error to observe.** The listener's `allowedRoutes.namespaces.from: Selector` does not match the route's namespace labels, so the Gateway never admitted it; requests fall through to whatever else matches.
- **`ResolvedRefs: False` while the route is admitted.** A `backendRef` names a Service in another namespace, or `certificateRefs` names a Secret in another namespace, and no `ReferenceGrant` exists in that target namespace permitting the reference.
- **The route attaches but never matches.** The route's `hostnames` do not intersect the listener's `hostname`; the intersection, not the route's list alone, determines which requests reach the rules.
- **Weighted `backendRefs` distribute nothing.** A weight of 0 on one backend removes it from selection entirely rather than sending a small share, and weights are relative within a single rule, not percentages across rules.
- **A feature present in the CRDs is rejected by the controller.** The Experimental channel installs fields that a Standard-channel implementation does not implement; conformance reports, not CRD presence, indicate support.
- **Deleting the Gateway leaves orphaned routes.** HTTPRoutes referencing a removed parent persist as objects with unsatisfied `parentRefs`, so `kubectl get httproute` still lists routes that serve no traffic.
