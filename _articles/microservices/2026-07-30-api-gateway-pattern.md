---
title: "The API gateway: one front door, and the trap of putting logic in it"
date: 2026-07-30
track: microservices
summary: "An API gateway sits between clients and your services doing the cross-cutting work nobody wants to repeat: TLS termination, auth, rate limiting, routing. Newman's warning is that it's a magnet for business logic it should never hold. Here's what belongs in it, what doesn't, and a minimal routing config."
reading_time: 5
tags: [api-gateway, microservices, routing, rate-limiting, bff, envoy]
sources:
  - title: "Building Microservices, 2nd ed. — Sam Newman (API gateways & service meshes)"
    url: "https://samnewman.io/books/building_microservices_2nd_edition/"
  - title: "Pattern: API Gateway / Backends for Frontends — microservices.io (Chris Richardson)"
    url: "https://microservices.io/patterns/apigateway.html"
  - title: "Envoy: HTTP routing — envoyproxy.io documentation"
    url: "https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/http/http_routing"
  - title: "Kubernetes Gateway API — gateway-api.sigs.k8s.io"
    url: "https://gateway-api.sigs.k8s.io/"
---

The moment you split a monolith into services, clients face a new problem: which of these twenty hostnames do I call, and do I really have to implement TLS, auth, and retries against each one? The API gateway is the standard answer — a single entry point that sits at the edge of your system and handles the cross-cutting concerns so neither the client nor every individual service has to.

## What actually belongs in the gateway

Newman is precise about the gateway's job: it's a *reverse proxy* doing **cross-cutting, request-level plumbing**. The uncontroversial list:

- **TLS termination** — one place holds the certs.
- **Authentication** — validate the JWT or API key once, at the edge, and pass a trusted identity header downstream so services don't each re-verify signatures.
- **Rate limiting** — protect the whole system from a client hammering it.
- **Routing** — map `POST /orders` to the orders service, `/catalog/*` to the catalog service.
- **Observability** — a natural choke point to emit a request ID, latency, and status for every call.

A minimal Envoy route config shows the shape — match a path prefix, forward to an upstream cluster:

```yaml
route_config:
  virtual_hosts:
    - name: edge
      domains: ["api.example.com"]
      routes:
        - match: { prefix: "/orders" }
          route:
            cluster: orders_service
            timeout: 3s
            retry_policy:
              retry_on: "5xx,reset"
              num_retries: 2
        - match: { prefix: "/catalog" }
          route: { cluster: catalog_service, timeout: 1s }
```

That's the whole idea: a declarative table of "this path → that service," plus timeouts and retries at the edge. On Kubernetes the same concept is now standardized as the **Gateway API** (the successor to Ingress), where `HTTPRoute` resources express these rules and different implementations (Envoy Gateway, Istio, NGINX) satisfy them.

## The trap Newman keeps hammering

Here's the failure mode that turns a gateway into a liability: because *every* request flows through it, it becomes irresistible to stuff business logic there. Someone adds "if the order total is over $500, also call the fraud service." Someone else adds response aggregation and field-level transformation. Six months later the gateway is a distributed monolith's worst nightmare — a single component that every team must coordinate to change, owned by no team, that you can't deploy without a cross-team review.

Newman's rule: **keep the gateway dumb.** Cross-cutting request plumbing, yes. Anything that encodes what your product *does*, no. The test is ownership: if changing a business rule means editing the gateway, the boundary is wrong.

## Gateway vs. BFG vs. mesh — don't conflate them

Three things get muddled here, and keeping them distinct saves you architecture arguments:

- **API gateway** — north-south traffic (clients → system). One shared front door. Generic.
- **Backend-for-Frontend (BFF)** — also north-south, but *per client type*. A gateway is generic; a BFF is opinionated toward one UI (mobile vs. web), doing aggregation and shaping *for that client*. Covered in an earlier article here. If you find yourself wanting aggregation in the gateway, you probably want a BFF instead.
- **Service mesh** — east-west traffic (service → service). mTLS, retries, and load balancing *between* internal services, usually via sidecars or a sidecarless data plane. The gateway guards the perimeter; the mesh governs the interior.

A common, healthy setup runs all three: a thin gateway at the edge for auth and routing, BFFs behind it for client-specific shaping, and a mesh inside for service-to-service resilience.

## The one real risk

A single front door is also a single point of failure. Route every request through one component and its availability caps your whole system's availability, and its latency adds to every call. So the gateway must be horizontally scaled and boring — no per-request state, generous timeouts, health checks — precisely so it can stay dumb and always up. The reliability argument is *another* reason to keep logic out: the less it does, the less there is to break on the one path every request must take.

**Try next:** Stand up Envoy (or Envoy Gateway on k8s) in front of two toy services, put a 2-req/sec rate limit and JWT validation at the edge, and confirm both services receive a pre-validated identity header and never see an unauthenticated request — then resist the urge to add your first `if` about business data, and write down where that logic should live instead.
