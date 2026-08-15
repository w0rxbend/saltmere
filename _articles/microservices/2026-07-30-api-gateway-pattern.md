---
title: "The API gateway: one front door, and the trap of putting logic in it"
date: 2026-07-30
track: microservices
summary: "An API gateway sits between clients and services doing the cross-cutting work nobody wants to repeat: TLS termination, authentication, rate limiting, routing. Newman's warning is that it is a magnet for business logic it should never hold. What belongs in it, what does not, and a minimal routing config."
reading_time: 6
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

**Gist.** Splitting a monolith into services multiplies the endpoints a client must address and duplicates transport-layer security (TLS), authentication, and retry handling across every one of them. An API gateway collapses that into a single edge component: a reverse proxy holding a declarative route table plus cross-cutting request plumbing, so neither clients nor individual services repeat the work. The cost is that every request now traverses one shared component, whose availability bounds system availability and whose latency is added to every call — and which attracts business logic that no single team then owns.

## The gateway's job

Newman describes the gateway as a *reverse proxy* performing **cross-cutting, request-level plumbing**. The uncontested responsibilities:

- **TLS termination** — one component holds the certificates and negotiates the handshake, rather than each service terminating its own.
- **Authentication** — validate the JSON Web Token (JWT) or application programming interface (API) key once, at the edge, and forward a trusted identity header downstream, so no service repeats signature verification.
- **Rate limiting** — bound the request rate a single client can impose on the system as a whole.
- **Routing** — map `POST /orders` to the orders service and `/catalog/*` to the catalog service.
- **Observability** — the edge is the one point every request crosses, so a request identifier, latency and status can be emitted there for every call without instrumenting each service.

Each item shares a property: it is a function of the request envelope — headers, method, path, source — and never of the request *body's* domain meaning. That property is the boundary the rest of the article defends.

A minimal Envoy route configuration shows the shape. A match predicate selects a route; the route names an upstream cluster and the edge policies applied to it:

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

The structure is a declarative table of path → cluster, with per-route timeouts and retries attached. Note that the two routes carry **different budgets**: the orders route allows 3 s and up to 2 retries, the catalog route 1 s and none. Envoy's route `timeout` is the overall upstream timeout for the request, and retries happen inside it rather than restarting it; bounding each individual attempt requires the retry policy's separate `per_try_timeout`. A gateway that instead applies its timeout per attempt multiplies worst-case latency by the number of attempts, which is the arithmetic the next section's sketch makes explicit.

On Kubernetes the same concept is standardised as the **Gateway API**, the successor to Ingress, in which `HTTPRoute` resources express the routing rules and multiple implementations — Envoy Gateway, Istio, NGINX — satisfy the same resource definitions. The specification splits the resources by role: `Gateway` describes the listener an infrastructure provider runs, while `HTTPRoute` describes the rules attached to it, and the same `HTTPRoute` is accepted by any conformant implementation.

### Implementation sketch (Scala)

The load-bearing mechanism is prefix matching with a deterministic tie-break, followed by policy application. Envoy resolves ambiguity by the order routes are declared; the sketch below instead resolves it by longest prefix, which makes the outcome independent of table order.

```scala
final case class Route(
    prefix: String,
    cluster: String,
    timeout: FiniteDuration,
    retries: Int
)

final case class Request(path: String, headers: Map[String, String])

final class RouteTable(routes: Vector[Route]):
  // Longest prefix wins, so adding a more specific route cannot be shadowed
  // by a broader one declared earlier.
  private val byLength = routes.sortBy(-_.prefix.length)

  def resolve(path: String): Option[Route] =
    byLength.find(r => path.startsWith(r.prefix))

def handle(
    table: RouteTable,
    authenticate: Request => Option[String],
    send: (Route, Request) => Either[Throwable, Int]
)(req: Request): Int =
  table.resolve(req.path) match
    case None => 404
    case Some(route) =>
      authenticate(req) match
        case None => 401
        case Some(subject) =>
          val forwarded =
            req.copy(headers = req.headers + ("x-identity" -> subject))
          // Lazy, so attempts stop at the first non-5xx result. Timeout
          // enforcement is elided; route.timeout would bound each attempt here.
          LazyList
            .range(0, route.retries + 1)
            .map(_ => send(route, forwarded))
            .collectFirst { case Right(status) if status < 500 => status }
            .getOrElse(502)
```

Nothing in `handle` inspects the request body. That is the invariant: **the gateway's decision function depends only on path, headers and route configuration.** Once a branch reads a domain field, the invariant is gone and the component has acquired a second owner.

## The failure mode

Because every request flows through it, the gateway is the most convenient place to put anything. A rule appears: if the order total exceeds some threshold, call the fraud service as well. Response aggregation follows, then field-level transformation. The end state is a component that every team must coordinate to change, that no single team owns, and that cannot be deployed without cross-team review — the coupling a service split was meant to remove, reintroduced at the edge.

Newman's guidance is to **keep the gateway dumb**: cross-cutting request plumbing, yes; anything encoding what the product does, no. The operational test is ownership. If changing a business rule requires editing gateway configuration, the boundary is in the wrong place.

## Gateway, BFF and mesh are three different things

Three components are routinely conflated. They differ in traffic direction and in generality:

- **API gateway** — north-south traffic (clients into the system). One shared front door, generic across clients.
- **Backend-for-Frontend (BFF)** — also north-south, but **one per client type**. Where a gateway is generic, a BFF is opinionated toward a single user interface — mobile or web — performing aggregation and response shaping for that client alone. A desire for aggregation in the gateway is a signal that a BFF is the missing component.
- **Service mesh** — east-west traffic (service to service). Mutual TLS, retries and load balancing *between* internal services, implemented via sidecar proxies or a sidecarless data plane. The gateway guards the perimeter; the mesh governs the interior.

The three compose: a thin gateway at the edge for authentication and routing, BFFs behind it for client-specific shaping, and a mesh inside for service-to-service resilience.

## Availability arithmetic

A single front door is also a single point of failure. When every request traverses one component, that component's availability is an upper bound on the system's availability, and its per-request latency is added to every call regardless of which upstream serves it. The mitigation is structural rather than clever: run the gateway **horizontally scaled and stateless per request**, so any instance can serve any request and instances can be replaced without draining session state, with health checks removing failed instances from rotation.

Reliability therefore reinforces the ownership argument. The less the gateway does, the smaller the set of things that can fail on the one path every request must take.

## Pitfalls

- **Prefix routes matched in declaration order** — a broad `/` or `/api` route declared before `/api/orders` swallows the specific route entirely, and the specific service receives no traffic while the configuration appears correct.
- **Edge retries stacked on service retries** — the gateway retries twice, the service retries twice internally, and a single client request becomes a multiplied load on the failing upstream at the moment it is least able to absorb it.
- **Timeout shorter upstream than at the edge** — the upstream abandons work the gateway is still waiting on, so the client sees the gateway's longer timeout rather than the fast failure the upstream produced.
- **Authentication at the edge without network isolation** — services trust the forwarded identity header, so any caller able to reach a service directly can set that header and bypass authentication entirely.
- **Business branches in gateway configuration** — a rule reading a domain field means a business change now requires a gateway deployment, and the release cadence of every team is coupled to the edge component's review process.
- **Per-request state in the gateway** — sticky sessions or in-process counters prevent instances from being interchangeable, so losing one instance loses the requests bound to it and horizontal scaling no longer restores capacity uniformly.
