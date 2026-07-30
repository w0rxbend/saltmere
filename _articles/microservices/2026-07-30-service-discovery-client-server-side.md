---
title: "Service Discovery: Client-Side vs Server-Side"
date: 2026-07-30
track: microservices
summary: "In an orchestrated fleet, instance IPs and ports change every deploy. Two patterns answer 'where is service X right now?' — the client queries a registry and load-balances itself, or it hits a stable endpoint that resolves for it. This walks the registry, health checks, and the trade-off, with a Kubernetes DNS lookup and a Consul catalog query."
reading_time: 5
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

In a static datacenter you hardcoded `10.0.3.7:8080` and moved on. In an orchestrated fleet that address is a lie by the next deploy: instances are scheduled onto whatever node has room, ports are assigned dynamically, autoscaling adds and drops replicas, and a crashed pod comes back somewhere else. The question "where is service X right now?" has to be answered at call time, not at build time. That is **service discovery**, and it hinges on one shared component and one architectural choice.

## The service registry

The shared component is a **service registry**: a database of live instances keyed by service name. Instances register on startup and deregister on shutdown (or are evicted when they stop passing health checks), and callers look up the current set. As microservices.io puts it, this exists because "the number of services instances and their locations changes dynamically." Everything below is just *who talks to the registry* — the client, or something in front of the client.

## Client-side discovery

The client asks the registry directly, gets back the list of healthy instances, and picks one itself. "When making a request to a service, the client obtains the location of a service instance by querying a Service Registry... [then] uses a load-balancing algorithm to select one of the available service instances and makes a request."

The canonical stack is Netflix's: **Eureka** as the registry, **Ribbon** (now Spring Cloud LoadBalancer) as the in-process client that caches the instance list and round-robins across it. The load balancing happens inside the calling process — there is no proxy hop.

- **Upside:** one fewer network hop and no shared LB to provision. The client sees every instance, so it can do smart routing — zone affinity, weighted, least-connections.
- **Downside:** "the client is coupled to the Service Registry [and] you need to implement client-side service discovery logic for each programming language and framework." A polyglot fleet needs a discovery client per language, which is exactly the tax that pushed teams toward the next pattern.

## Server-side discovery

The client makes a plain request to a stable endpoint — a DNS name or a load balancer — and something else does the lookup. "The client makes a request to a service via a load balancer... [which] queries the service registry and routes each request to an available service instance." The client stays dumb; it never learns the registry exists.

**Kubernetes** is the everyday example. You declare a `Service`, which gets a stable virtual IP and a stable DNS name; the cluster DNS (CoreDNS) and kube-proxy handle resolution and balancing across the backing pods. The registry is the Kubernetes API + endpoints; the "router" is per-node kube-proxy.

```yaml
apiVersion: v1
kind: Service
metadata:
  name: orders
  namespace: shop
spec:
  selector:
    app: orders            # registry = pods matching this label
  ports:
    - name: http
      port: 80             # stable port clients dial
      targetPort: 8080     # actual container port (may differ per pod)
```

A caller just resolves the name. From any pod in the cluster:

```console
$ dig +short orders.shop.svc.cluster.local
10.96.14.201                 # the Service's stable ClusterIP

# named-port SRV record resolves port too, no hardcoding
$ dig +short SRV _http._tcp.orders.shop.svc.cluster.local
0 100 80 orders.shop.svc.cluster.local.
```

The name follows the fixed `<service>.<namespace>.svc.cluster.local` scheme, so `orders` in namespace `shop` is always reachable at `orders.shop` — no registry client, no SDK, works from any language that can open a socket. (A *headless* Service — `clusterIP: None` — instead returns one A record per pod, handing the raw instance set back to the client if you want to balance yourself; that is the escape hatch back to client-side.)

## Health checks and TTLs

A registry full of dead instances is worse than no registry. Entries must expire, which is where health checks and TTLs come in. **Consul** models this explicitly: an agent runs checks (HTTP, TCP, gRPC, or a TTL the service must heartbeat), and instances failing checks are excluded from queries. You read the live set straight off the catalog over HTTP:

```console
$ curl -s http://127.0.0.1:8500/v1/catalog/service/orders | \
    jq '.[] | {node: .Node, addr: .ServiceAddress, port: .ServicePort}'
{ "node": "web-01", "addr": "10.0.3.7", "port": 8080 }
{ "node": "web-02", "addr": "10.0.4.2", "port": 8080 }
```

Notice this is *client-side* discovery in raw form — the caller pulls the instance list and chooses. Consul can also front the same catalog with DNS (`orders.service.consul`), giving you the server-side shape from the identical registry. The pattern is a client choice, not a property of the tool.

Two knobs govern staleness. **Check interval** decides how fast a dead instance is noticed; **TTL / deregister-after** decides how long a flapping one lingers. Set them too loose and you route to corpses; too tight and a slow GC pause evicts a healthy node mid-request. This is the same freshness-vs-noise tension the [circuit breaker](/articles/microservices/2026-07-24-circuit-breakers-resilience4j/) exists to paper over when discovery is momentarily wrong.

## The trade-off

| | Client-side (Eureka/Ribbon, Consul HTTP) | Server-side (K8s Service, ELB, Consul DNS) |
|---|---|---|
| Who queries the registry | the caller | a router / DNS in front of it |
| Network hops | fewer (direct) | one more (via router) |
| Client complexity | discovery SDK per language | none — plain request |
| Routing smarts | rich (client sees all instances) | whatever the router offers |
| Coupling | client ↔ registry | client ↔ stable endpoint |

Newman, in *Building Microservices* (2nd ed.), frames the practical default: DNS-plus-load-balancer server-side discovery is the low-friction choice because it needs nothing special in the client, and a dedicated registry like Consul or Eureka earns its keep once you want richer, faster, health-aware routing than DNS TTLs can give. In practice most teams on Kubernetes get server-side discovery for free from the platform and never run a separate registry at all.

This is deliberately the *plumbing* layer — finding an instance. Deciding call policy across those instances (mTLS, retries, traffic splitting) is the [service mesh](/articles/microservices/2026-07-26-istio-ambient-mesh-sidecarless/)'s job, and exposing services to outside clients is the API gateway's; both sit a layer above what discovery answers.

**Try next:** In a kind or minikube cluster, `kubectl run tmp --rm -it --image=nicolaka/netshoot -- dig orders.shop.svc.cluster.local`, then scale the Deployment (`kubectl scale deploy/orders --replicas=3`) and re-run `dig` against the *headless* variant — watch the A-record set grow while the ClusterIP name stays a single stable address. That difference *is* server-side vs client-side, live.
