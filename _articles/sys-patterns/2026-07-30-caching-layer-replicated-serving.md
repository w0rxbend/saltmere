---
title: "The Caching Layer: A Serving Pattern Bolted On Without Touching the Replicas"
date: 2026-07-30
track: sys-patterns
summary: "Burns treats the cache as a reusable, composable serving component placed between the load balancer and a replicated stateless service. A close read of where it goes, cache-aside vs. a transparent HTTP proxy, sidecar vs. shared tier, the hit-rate arithmetic that doubles as rate limiting, and stale-while-revalidate — with a full nginx proxy_cache config."
reading_time: 7
tags: [caching, nginx, varnish, load-balancing, rate-limiting, stale-while-revalidate, burns]
sources:
  - title: "Designing Distributed Systems, 2nd ed. — Ch. 6, Replicated Load-Balanced Services & the Caching Layer (Burns, O'Reilly)"
    url: "https://www.oreilly.com/library/view/designing-distributed-systems/9781098156343/ch06.html"
  - title: "Designing Distributed Systems (free eBook, Microsoft) — 'Introducing a Caching Layer' / 'Deploying Your Cache'"
    url: "https://info.microsoft.com/rs/157-GQE-382/images/EN-CNTNT-eBook-DesigningDistributedSystems.pdf"
  - title: "Module ngx_http_proxy_module — proxy_cache_* directives (nginx.org)"
    url: "https://nginx.org/en/docs/http/ngx_http_proxy_module.html"
  - title: "The fundamentals of web proxy caching with Varnish (Varnish Cache docs)"
    url: "https://varnish-cache.readthedocs.io/tutorial/introduction.html"
  - title: "RFC 5861 — HTTP Cache-Control Extensions for Stale Content (stale-while-revalidate, stale-if-error)"
    url: "https://httpwg.org/specs/rfc5861.html"
---

**Gist.** In the [replicated load-balanced serving pattern](/articles/sys-patterns/2026-07-26-replicated-load-balanced-serving/), every request — including the ten-thousandth identical request for the same product page in the same second — reaches a replica and repeats the full amount of work. Burns' answer in *Designing Distributed Systems* is a **caching layer** inserted between the load balancer's public entry and the replica pool, a generic component that serves repeated reads without waking the service behind it. The cost is a second copy of the data with an independent lifetime: every cached entry can be stale, and invalidation becomes an explicit operational duty rather than an implicit consequence of recomputation.

## The layer inserted without touching the replicas

The cache sits *in front of* the replicas and *behind* the public entry point. A request reaches the cache first and only reaches a replica on a miss. The replicas remain as stateless and interchangeable as before; **no code in the service changes, and the service holds no reference to the cache**.

That property is what makes caching a *pattern* rather than a feature. As with the [sidecar](/articles/sys-patterns/2026-07-24-sidecar-pattern/) and [ambassador](/articles/sys-patterns/2026-07-24-ambassador-pattern-sharded-backend/) patterns, the component is generic: the same Varnish or nginx image fronts a catalog application programming interface (API) today and an authentication service tomorrow. The layer is composed in; the thing it fronts is not rewritten.

## Cache-aside versus a transparent HTTP proxy

Two designs add caching, and they occupy different positions in the architecture.

- **Cache-aside (application-managed).** The replica queries a cache such as **Redis** for a key; on a miss it computes the value, writes it back, and returns it. The cache is a *data-tier* dependency of the replica, and the lookup, write-back, time-to-live (TTL) and invalidation logic live in application code. This admits caching of arbitrary computed objects rather than only HyperText Transfer Protocol (HTTP) responses, at the price of **that logic existing in every replica**.
- **Transparent HTTP caching proxy.** **Varnish** or nginx's `proxy_cache` sits in the request path, parses HTTP, keys responses by uniform resource locator (URL) plus any configured additions, honours `Cache-Control`, and serves hits without contacting a replica. The Varnish documentation describes it as a "web application accelerator": a reverse proxy between clients and the backend. **The application is oblivious.**

Burns' serving-patterns framing works with the transparent proxy, the form that composes as a drop-in layer. The remainder of this article stays there. Cache-aside remains the applicable option when the cached item is a computed object rather than an HTTP response.

## Placement: shared tier versus sidecar

One component, two deployment topologies.

| | **Shared caching tier** | **Caching sidecar** |
|---|---|---|
| Placement | Separate replicated pool of cache nodes between the load balancer and the service | A cache container in *every* service pod |
| Hit rate | Higher — one pool, one key space, requests converge | Lower per pod — each replica warms its own cache from cold |
| Cost of a stale key | One shared copy to invalidate | Fan-out: N copies, N TTLs expiring independently |
| Network hop | Extra hop to the cache tier | `localhost` — cache shares the pod's loopback interface |
| Applicable when | Read-heavy, hot key set shared across clients | Per-replica locality matters, or no cross-node dependency is acceptable |

The sidecar form is the [sidecar pattern](/articles/sys-patterns/2026-07-24-sidecar-pattern/) applied to caching: a second container in the pod, reachable over `localhost`, intercepting the replica's traffic. It introduces no shared dependency, but **N independent caches give a lower aggregate hit rate and N independent places a stale entry can survive**. A shared tier exchanges the extra hop for a single, denser key space, which favours read-heavy services whose working set is common to all clients.

## Hit-rate arithmetic, and the same mechanism as a rate limiter

If the cache serves a fraction **h** of requests — the **hit rate** — the backend receives the complement:

```
backend_rps = incoming_rps × (1 − h)
```

At 10,000 requests per second (rps) and h = 0.95, the replicas field **500 rps** rather than 10,000: a 20× reduction in required backend capacity. Raising h from 0.95 to 0.99 divides the residual by a further factor of five, 500 rps to 100 rps. **The quantity being reduced is (1 − h), so equal absolute increments in h near 1 cut backend load by successively larger factors** — which is why hit rate is the number worth tuning.

The same mechanism acts as a **rate limiter and denial-of-service (DoS) shield**, the aspect Burns emphasises. With request coalescing enabled — nginx's `proxy_cache_lock`, Varnish's request coalescing — a hot key has **at most one fill in flight, however many callers are waiting on it**. A flood of one million concurrent requests for one URL collapses into a single origin request; the remainder are served from cache or parked on the in-flight fill. The invariant is per key: **at most one outstanding backend fetch**. Caching therefore protects capacity structurally, not only latency.

### Implementation sketch (Scala)

The coalescing invariant is the load-bearing idea, and it is expressible with a concurrent map of in-flight results. `compute` decides a single winner per key; every other caller receives the winner's `Future`.

```scala
import java.util.concurrent.ConcurrentHashMap
import scala.concurrent.{Future, ExecutionContext}

final class CoalescingCache[K, V](fetch: K => Future[V], ttlMillis: Long)
                                 (using ec: ExecutionContext):

  private final case class Entry(value: Future[V], expiresAt: Long)

  private val entries = ConcurrentHashMap[K, Entry]()

  def get(key: K): Future[V] =
    val now = System.currentTimeMillis()
    // Atomic per key: exactly one caller runs the remapping function,
    // so exactly one backend fetch is in flight for a missing key.
    val entry = entries.compute(key, (k, current) =>
      if current != null && current.expiresAt > now then current
      else Entry(fetch(k), now + ttlMillis)
    )
    // Outside compute: ConcurrentHashMap forbids re-entrant modification
    // from within a remapping function.
    entry.value.failed.foreach(_ => entries.remove(key, entry))
    entry.value
```

The entry stores the *unresolved* `Future`, not the value: concurrent callers arriving during the fill join the pending computation instead of starting their own. Removing the entry on failure prevents a transient backend error from being retained for the whole TTL.

## TTL, invalidation, and stale-while-revalidate

Every cached entry carries a **TTL**, the interval during which it is considered fresh. Short TTLs yield fresher data and a lower hit rate; long TTLs invert both. The harder problem is **invalidation**: when the underlying data changes before expiry, the key must be purged or overwritten actively. In a shared tier that is one purge; in a sidecar deployment it is a fan-out to every pod.

**Stale-while-revalidate** ([RFC 5861](https://httpwg.org/specs/rfc5861.html)) removes the penalty imposed on the request that arrives at the instant of expiry, which otherwise pays the full backend latency. The cache returns the *stale* copy immediately and refreshes the entry in the background. `Cache-Control: max-age=600, stale-while-revalidate=30` declares the response fresh for 600 seconds, after which stale content may be served for a further 30 seconds while revalidation proceeds. The companion directive `stale-if-error` permits serving stale content when the backend returns a server error, which converts the cache into a buffer during an origin outage.

## Concrete: nginx proxy_cache in front of the pool

The `upstream` block is the replicated pool from the base pattern; nginx acts as load balancer and caching layer in one process.

```nginx
# Shared on-disk cache: 10MB of keys in memory (~80k keys), 1GB of bodies.
proxy_cache_path /var/cache/nginx/catalog levels=1:2
                 keys_zone=catalog:10m inactive=60m max_size=1g;

upstream catalog_replicas {          # the replicated stateless pool
    server 10.0.0.11:8080;
    server 10.0.0.12:8080;
    server 10.0.0.13:8080;
}

server {
    listen 80;

    location / {
        proxy_pass  http://catalog_replicas;
        proxy_cache catalog;
        proxy_cache_key "$scheme$host$request_uri";

        proxy_cache_valid 200 302 10m;   # TTL for OK/redirects
        proxy_cache_valid 404      1m;   # negative responses, shorter TTL

        # Coalesce concurrent misses on the same key into ONE backend fetch.
        proxy_cache_lock on;
        proxy_cache_lock_timeout 5s;

        # stale-while-revalidate + stale-if-error, served from cache:
        proxy_cache_use_stale updating error timeout
                              http_500 http_502 http_503 http_504;
        proxy_cache_background_update on;

        add_header X-Cache-Status $upstream_cache_status;  # HIT/MISS/UPDATING
    }
}
```

`proxy_cache_lock on` supplies the coalescing invariant: concurrent misses on one key become a single upstream request. `proxy_cache_use_stale updating` together with `proxy_cache_background_update on` is nginx's realisation of stale-while-revalidate — an expired entry is served immediately while a background subrequest refreshes it. The `X-Cache-Status` header makes h measurable: the ratio of `HIT` to total responses is the h in the formula above.

Nothing in the `catalog_replicas` pool changed. The cache is a layer, and layers compose.

## Pitfalls

- **A cache key that omits a varying request dimension serves one client's response to another.** `"$scheme$host$request_uri"` ignores headers; if the backend varies output by `Accept-Language`, `Authorization` or a cookie, that dimension must enter the key or a `Vary` header must be honoured.
- **`proxy_cache_lock_timeout` expiring converts coalescing back into a stampede.** Waiters that exceed the timeout are released to the backend individually, so a backend fill slower than the timeout produces the thundering herd the lock was configured to prevent.
- **A cached error response persists for its full TTL.** `proxy_cache_valid 404 1m` is deliberate; caching a transient 5xx for the success TTL keeps a resolved outage visible until expiry.
- **Sidecar deployment multiplies invalidation.** A purge that reaches one pod leaves N − 1 caches serving the old value until their own TTLs expire, so the effective staleness window is the TTL, not the purge latency.
- **`stale-if-error` masks a failing backend from external observation.** Clients continue receiving successful responses while the origin returns errors, so alerting must derive from origin health and cache status rather than edge status codes.
- **Reported hit rate is inflated when it counts `UPDATING` responses as hits.** They are served from cache but each still corresponds to a background origin fetch, so origin load exceeds `incoming_rps × (1 − h)` computed from that figure.
