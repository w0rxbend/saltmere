---
title: "The Caching Layer: A Serving Pattern You Bolt On Without Touching the Replicas"
date: 2026-07-30
track: sys-patterns
summary: "Burns treats the cache as a reusable, composable serving component you drop between the load balancer and a replicated stateless service. A close read of why it goes there, cache-aside vs. a transparent HTTP proxy, sidecar vs. shared tier, the hit-rate math that doubles as rate limiting, and stale-while-revalidate — with a full nginx proxy_cache config."
reading_time: 5
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

You already have the base case running: N identical stateless replicas behind a load balancer, scaled by a replica count (that's the [replicated load-balanced serving pattern](/articles/sys-patterns/2026-07-26-replicated-load-balanced-serving/), covered separately). It works, but every request — including the ten thousandth identical request for the same product page this second — travels all the way to a replica and does the full amount of work. Burns' answer in *Designing Distributed Systems* is not to make the replicas smarter. It's to insert a **caching layer** between the load balancer and the pool: a reusable, composable serving component you bolt on **without changing a line of the service behind it**.

## The layer you insert without touching the replicas

The cache goes *in front of* the replicas and *behind* the load balancer's public entry. A request arrives, hits the cache first, and only reaches a replica on a miss. The replicas stay exactly as stateless and interchangeable as before — they don't know a cache exists.

That "don't know it exists" property is the whole point of treating caching as a **pattern** rather than a feature. Like the [sidecar](/articles/sys-patterns/2026-07-24-sidecar-pattern/) and [ambassador](/articles/sys-patterns/2026-07-24-ambassador-pattern-sharded-backend/) patterns, the value is that the component is generic: the same Varnish or nginx image caches a catalog API today and an auth service tomorrow. You compose it in; you don't rewrite the thing it fronts.

## Cache-aside vs. a transparent HTTP proxy

There are two fundamentally different ways to add caching, and they land in different places in the architecture.

- **Cache-aside (application-managed).** Your code asks a cache like **Redis** for a key; on a miss it computes the value, writes it back, and returns it. The cache is a *data-tier* dependency the replica talks to — logic lives in the application. This is flexible (you cache computed objects, not just HTTP responses) but the invalidation and TTL logic is now your problem, in your code, in every replica.
- **A transparent HTTP caching proxy.** You put **Varnish** or **nginx `proxy_cache`** in the request path. It parses HTTP, keys responses by URL (plus whatever you add), honors `Cache-Control`, and serves hits without ever waking a replica. Varnish's docs frame it plainly: it's a "web application accelerator" — a reverse proxy that sits between clients and the backend and short-circuits repeated work. The application stays oblivious.

Burns' serving-patterns framing favors the transparent proxy, because it's the version that composes as a drop-in layer. The rest of this article focuses there; reach for cache-aside/Redis when what you're caching is a *computed object*, not an HTTP response.

## Where the cache sits: shared tier vs. sidecar

Same component, two deployment topologies:

| | **Shared caching tier** | **Caching sidecar** |
|---|---|---|
| Placement | A separate replicated pool of cache nodes between LB and service | A cache container in *every* service pod |
| Hit rate | Higher — one pool, one key space, requests converge | Lower per-pod — each replica warms its own cache from cold |
| Blast radius of a stale key | One shared copy to invalidate | Fan-out: N copies, N TTLs drifting independently |
| Network hop | Extra hop to the cache tier | `localhost` — cache shares the pod's loopback |
| Best when | Read-heavy, hot key set shared across all clients | Per-replica locality matters, or you want zero cross-node dependency |

The sidecar version is the [sidecar pattern](/articles/sys-patterns/2026-07-24-sidecar-pattern/) applied to caching: a second container in the pod, reachable over `localhost`, that intercepts the replica's egress or ingress. It's operationally simple and adds no shared dependency — but N independent caches mean a lower aggregate hit rate and N places a stale entry can hide. A shared tier trades that extra hop for a single, denser key space. Read-heavy services with a hot common working set usually win with the shared tier.

## The hit-rate math is also your rate limiter

The economics are a one-liner. If the cache serves a fraction **h** of requests (the **hit rate**), the backend only sees **(1 − h)** of the traffic:

```
backend_rps = incoming_rps × (1 − h)
```

At 10,000 rps and a 95% hit rate, your replicas field **500 rps**, not 10,000 — a 20× reduction in required capacity. Push the hit rate from 95% to 99% and backend load *halves again* (500 → 100 rps). This is why hit rate is the number to tune: small gains near the top compound hard.

The same mechanism is a **rate limiter and DoS shield**, which is the part Burns emphasizes. With request coalescing turned on — nginx's `proxy_cache_lock`, Varnish's request coalescing — a single hot key is fetched from the backend **at most once per TTL**, no matter how many clients ask for it simultaneously. A million-request flood for one URL collapses into *one* origin request; the other 999,999 are served from cache or parked waiting on that single fill. The cache absorbs the spike so the replicas never feel it. Caching stops being just a latency optimization and becomes a structural protection for everything behind it.

## TTL, invalidation, and stale-while-revalidate

Every cached entry needs a **TTL** — how long it's considered fresh. Short TTLs mean fresher data and a lower hit rate; long TTLs mean the opposite. The genuinely hard problem is **invalidation**: when the underlying data changes before the TTL expires, you must actively purge or update the key, and in a shared tier that's one purge, while in a sidecar deployment it's a fan-out to every pod.

**Stale-while-revalidate** ([RFC 5861](https://httpwg.org/specs/rfc5861.html)) removes the cruelest tradeoff. Normally, the request unlucky enough to arrive the instant an entry expires pays the full backend latency. With `stale-while-revalidate`, the cache serves the *stale* copy immediately and refreshes it in the background — the user never waits on the origin. `Cache-Control: max-age=600, stale-while-revalidate=30` means "fresh for 600s, then for another 30s serve stale while you revalidate." Its sibling `stale-if-error` serves stale content when the backend returns 5xx, turning a cache into a brownout buffer during an outage.

## Concrete: nginx proxy_cache in front of the pool

Here the `upstream` block *is* the replicated pool from the base pattern; nginx is both the load balancer and the caching layer in one process.

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
        proxy_cache_valid 404      1m;   # cache misses briefly too

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

`proxy_cache_lock on` is the DoS shield — concurrent misses on one key become a single upstream request. `proxy_cache_use_stale updating` plus `proxy_cache_background_update on` is nginx's implementation of stale-while-revalidate: expired entries are served instantly while a background subrequest refreshes them. The `X-Cache-Status` header lets you *measure* the hit rate you're now tuning — grep it for `HIT` vs `MISS` and you've got the `h` in the formula above.

Nothing in the `catalog_replicas` pool changed. That's the pattern: the cache is a layer, and layers compose.

**Try next:** deploy the config above against a two- or three-replica backend, then `wrk` or `ab` the *same* URL from 200 concurrent connections and watch `X-Cache-Status` — you should see exactly one `MISS`, a burst of `UPDATING`/`HIT` as the lock coalesces the rest, and backend access logs showing a single origin fetch per TTL no matter how hard you flood it.
