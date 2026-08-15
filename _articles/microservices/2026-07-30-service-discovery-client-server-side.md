---
title: "Service Discovery: Client-Side vs Server-Side"
date: 2026-07-30
track: microservices
summary: "In an orchestrated fleet, instance addresses and ports change every deploy. Two patterns answer 'where is service X right now?' — the caller queries a registry and load-balances itself, or it dials a stable endpoint that resolves on its behalf. This walks the registry, the health-check and time-to-live knobs, and the trade-off, with a Kubernetes DNS lookup and a Consul catalog query."
reading_time: 6
tags: [service-discovery, service-registry, kubernetes, consul, eureka, dns, load-balancing]
sources:
  - title: "Pattern: Client-side service discovery (microservices.io)"
    url: "https://microservices.io/patterns/client-side-discovery.html"
  - title: "Pattern: Server-side service discovery (microservices.io)"
    url: "https://microservices.io/patterns/server-side-discovery.html"
  - title: "DNS for Services and Pods (Kubernetes docs)"
    url: "https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/"
  - title: "Catalog HTTP API (HashiCorp Consul docs)"
    url: "https://developer.hashicorp.com/consul/api-docs/catalog"
  - title: "Consul catalog concept (HashiCorp Consul docs)"
    url: "https://developer.hashicorp.com/consul/docs/concept/catalog"
---

**Gist.** A hardcoded address such as `10.0.3.7:8080` stops being true as soon as a scheduler places an instance on a different node, assigns a dynamic port, or replaces a crashed replica. Service discovery answers "where is service X right now?" at call time by keeping a **service registry** of live instances keyed by service name, and by resolving that name either in the caller (client-side) or in a router in front of the caller (server-side). The cost is a second source of truth that can be stale in both directions: entries for instances that are already dead, and evictions of instances that are still healthy.

## The service registry

The registry is a database of live instances keyed by service name. Instances register on startup and deregister on shutdown, or are evicted when they stop passing health checks; callers read the current set. microservices.io states the motivation as the fact that "the number of services instances and their locations changes dynamically."

The registry is common to both patterns. The architectural choice is narrower than it first appears: **it is a choice about which component queries the registry**, the calling process or an intermediary.

## Client-side discovery

The caller queries the registry directly, receives the set of healthy instances, and selects one. In the pattern's own words, "when making a request to a service, the client obtains the location of a service instance by querying a Service Registry... [then] uses a load-balancing algorithm to select one of the available service instances and makes a request."

The canonical stack is Netflix's: **Eureka** as the registry and **Ribbon** (succeeded by Spring Cloud LoadBalancer) as the in-process client that caches the instance list and round-robins across it. Selection happens inside the calling process, so **there is no proxy hop between caller and callee**.

- **Benefit:** one fewer network hop, and no shared load balancer to provision. Because the caller holds the full instance set, it can apply routing policy that depends on that set — zone affinity, weighting, least-connections.
- **Cost:** the pattern's stated drawback is that the client is coupled to the service registry, and that client-side discovery logic must be implemented for each programming language and framework in use. A polyglot fleet therefore needs one discovery client per language runtime.

The cached instance list is the load-bearing state. It is refreshed on an interval, which means **every client holds a snapshot of the registry that is by construction older than the registry itself**; a freshly evicted instance stays selectable until the next refresh.

### Implementation sketch (Scala)

The mechanism worth making legible is the cached snapshot plus rotation, not the transport.

```scala
final case class Instance(host: String, port: Int)

/** Holds a snapshot of the registry and rotates over it. */
final class ClientSideBalancer(
    fetch: String => List[Instance],   // one registry query
    refreshAfter: java.time.Duration
):
  private val cursor = java.util.concurrent.atomic.AtomicInteger(0)
  @volatile private var snapshot: List[Instance] = Nil
  @volatile private var takenAt: java.time.Instant = java.time.Instant.MIN

  private def current(service: String): List[Instance] =
    val now = java.time.Instant.now()
    // Staleness is bounded by refreshAfter, never by the registry's own state.
    if takenAt.plus(refreshAfter).isBefore(now) then
      snapshot = fetch(service)
      takenAt = now
    snapshot

  def pick(service: String): Option[Instance] =
    val xs = current(service)
    if xs.isEmpty then None
    else Some(xs(Math.floorMod(cursor.getAndIncrement(), xs.size)))
```

`pick` returning `None` is the honest representation of an empty healthy set: a caller that instead falls back to the previous snapshot has chosen to route to instances the registry no longer lists.

## Server-side discovery

The caller issues a plain request to a stable endpoint — a DNS name or a load balancer — and the lookup happens elsewhere: "the client makes a request to a service via a load balancer... [which] queries the service registry and routes each request to an available service instance." The caller never learns that a registry exists.

**Kubernetes** is the common instance of the pattern. A declared `Service` receives a stable virtual IP address and a stable DNS name; the cluster DNS server (CoreDNS) resolves the name and kube-proxy balances across the backing pods. The registry is the Kubernetes API together with its endpoints; the router is the per-node kube-proxy.

```yaml
apiVersion: v1
kind: Service
metadata:
  name: orders
  namespace: shop
spec:
  selector:
    app: orders            # registry membership = pods matching this label
  ports:
    - name: http
      port: 80             # stable port callers dial
      targetPort: 8080     # container port (may differ per pod)
```

A caller resolves the name and nothing else. From a pod in the cluster:

```console
$ dig +short orders.shop.svc.cluster.local
10.96.14.201                 # the Service's stable ClusterIP

# named-port SRV record resolves the port as well
$ dig +short SRV _http._tcp.orders.shop.svc.cluster.local
0 100 80 orders.shop.svc.cluster.local.
```

The name follows the fixed `<service>.<namespace>.svc.cluster.local` scheme, so `orders` in namespace `shop` is reachable at `orders.shop` from any runtime that can open a socket — **no registry client and no software development kit (SDK) are required**. A *headless* Service (`clusterIP: None`) instead returns one A record per pod, which hands the raw instance set back to the caller; that is the documented route back to the client-side shape.

## Health checks and time-to-live

A registry that lists dead instances is worse than no registry, so entries must expire. **Consul** models expiry explicitly: an agent runs checks (HTTP, TCP, gRPC, or a time-to-live (TTL) check that the service must heartbeat). Which endpoint is queried decides whether check state is applied: the catalog endpoint below lists the registered instances of a service, while the health endpoint (`/v1/health/service/orders?passing`) filters to those whose checks are passing. The registered set is readable over HTTP:

```console
$ curl -s http://127.0.0.1:8500/v1/catalog/service/orders | \
    jq -c '.[] | {node: .Node, addr: .ServiceAddress, port: .ServicePort}'
{ "node": "web-01", "addr": "10.0.3.7", "port": 8080 }
{ "node": "web-02", "addr": "10.0.4.2", "port": 8080 }
```

That query is client-side discovery in raw form: the caller pulls the instance list and chooses. A caller that wants only passing instances must ask for them. Consul can also expose the same catalog through DNS (`orders.service.consul`), producing the server-side shape from an identical registry. **The pattern is a property of the caller's integration, not of the registry product.**

Two knobs govern staleness in opposite directions. The **check interval** bounds how quickly a failed instance is noticed; the **TTL or deregistration delay** bounds how long a flapping instance remains listed. Loose settings route traffic to instances that no longer serve; tight settings evict a healthy instance whose response was delayed by, for example, a garbage-collection pause. Both errors are transient and appear at the caller as failed requests, which is the window the [circuit breaker](/articles/microservices/2026-07-24-circuit-breakers-resilience4j/) covers.

## The trade-off

| | Client-side (Eureka/Ribbon, Consul HTTP) | Server-side (K8s Service, ELB, Consul DNS) |
|---|---|---|
| Who queries the registry | the caller | a router or DNS in front of it |
| Network hops | fewer (direct) | one more (via router) |
| Client complexity | discovery SDK per language | none — plain request |
| Routing policy | rich (caller sees all instances) | whatever the router offers |
| Coupling | caller ↔ registry | caller ↔ stable endpoint |

DNS plus a load balancer is the lower-friction default, because it requires nothing of the caller beyond opening a socket; a dedicated registry such as Consul or Eureka pays for its extra client only when routing policy must see the individual instances. On Kubernetes, server-side discovery arrives with the platform, so a separate registry is often not deployed at all.

Discovery answers only the location question. Call policy across the located instances — mutual TLS, retries, traffic splitting — belongs to the [service mesh](/articles/microservices/2026-07-26-istio-ambient-mesh-sidecarless/), and exposure to external clients belongs to the API gateway.

## Pitfalls

- **A cached instance list outlives the registry entry.** A client-side balancer that refreshes on an interval keeps selecting an instance for up to one refresh period after eviction; the symptom is connection refusals concentrated immediately after a scale-down.
- **A TTL check turns a paused process into a deregistration.** A stop-the-world pause longer than the TTL misses the heartbeat, so a healthy instance is removed from the catalog and must re-register.
- **A headless Service reintroduces client-side discovery unnoticed.** Setting `clusterIP: None` returns one A record per pod instead of a single virtual IP, so balancing and staleness handling silently become the caller's responsibility.
- **The catalog endpoint is not the health endpoint.** `/v1/catalog/service/<name>` answers with the registered instances of a service; a caller that wants only instances whose checks pass must query `/v1/health/service/<name>?passing` instead.
- **DNS caching sits below the registry.** A resolver or language runtime that caches an A record ignores subsequent registry changes for the cache lifetime, and the registry has no way to invalidate it.
- **Deregistration on shutdown is not guaranteed.** An instance killed without running its shutdown path never deregisters, so removal depends entirely on the health-check interval rather than on the graceful path.
- **Per-language discovery clients drift.** Each runtime carries its own refresh interval and selection algorithm, so traffic distribution differs by caller language even against one registry.
