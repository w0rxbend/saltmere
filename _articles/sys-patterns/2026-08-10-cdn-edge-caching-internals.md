---
title: 'CDN & Edge Caching Internals: The Request Path from PoP to Origin'
date: 2026-08-10
track: sys-patterns
summary: How a CDN actually caches at the edge — the request path through PoPs, mid-tier shields, and origin; cache keys and header normalization; Surrogate-Control vs Cache-Control precedence; HIT/MISS/EXPIRED and the Age header; tiered caching and origin shielding as a stampede defense at CDN scale; request collapsing; surrogate-key/cache-tag purge; and the dynamic-content escape hatches (ESI, stale-while-revalidate, compute@edge). With concrete Fastly, Cloudflare, Akamai, and Varnish/VCL examples.
reading_time: 6
tags:
- cdn
- edge-caching
- fastly
- cloudflare
- varnish
- surrogate-keys
- shielding
- request-collapsing
- cache-invalidation
- interview-prep
- caching
- cache-control
- edge
- system-design
sources:
- title: Fastly Documentation — Working with surrogate keys
  url: https://www.fastly.com/documentation/guides/full-site-delivery/purging/working-with-surrogate-keys/
- title: Fastly Documentation — Request collapsing
  url: https://www.fastly.com/documentation/guides/concepts/cache/request-collapsing/
- title: Fastly Documentation — Shielding
  url: https://www.fastly.com/documentation/guides/concepts/shielding/
- title: Fastly Documentation — Surrogate-Control header
  url: https://www.fastly.com/documentation/reference/http/http-headers/Surrogate-Control/
- title: Cloudflare Docs — Tiered Cache
  url: https://developers.cloudflare.com/cache/how-to/tiered-cache/
- title: Cloudflare Docs — Cache keys
  url: https://developers.cloudflare.com/cache/how-to/cache-keys/
- title: What is an Anycast Network? (Cloudflare Learning Center)
  url: https://www.cloudflare.com/learning/cdn/glossary/anycast-network/
- title: Using Amazon CloudFront Origin Shield (AWS documentation)
  url: https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/origin-shield.html
- title: Soft purges (Fastly documentation)
  url: https://www.fastly.com/documentation/guides/full-site-delivery/purging/soft-purges/
- title: Purging with surrogate keys (Fastly documentation)
  url: https://www.fastly.com/documentation/guides/full-site-delivery/purging/purging-with-surrogate-keys/
- title: RFC 5861 — HTTP Cache-Control Extensions for Stale Content
  url: https://datatracker.ietf.org/doc/html/rfc5861
---

A CDN is a cache with a map of the world stapled to it. The protocol-level rules — `Cache-Control`, `ETag`, freshness, revalidation — are covered in [HTTP caching semantics](/articles/microservices/2026-08-10-http-caching-cache-control-etag). This article is about what a content delivery network adds on top: a physical topology of caches, a key that decides what counts as "the same object," and controls that let you override the origin's opinion about caching entirely. The load-shedding math behind coalescing lives in [cache stampede & request coalescing](/articles/microservices/2026-08-10-cache-stampede-request-coalescing); here we look at how it plays out across PoPs.

## The request path

Follow a single request. A client resolves your hostname via anycast DNS and lands on the nearest **edge PoP** (point of presence) — Cloudflare and Fastly each run hundreds. The edge checks its local cache. On a **HIT**, the object is served immediately and the story ends in a few milliseconds.

On a **MISS**, the edge does *not* necessarily go to origin. In a tiered topology it forwards to a **mid-tier / shield PoP** — a cache chosen for its proximity to your origin. Only if the shield also misses does a request cross the open internet to your **origin**:

```
client → edge PoP → (shield / upper-tier PoP) → origin
```

Each hop is a cache; each layer that answers is a layer origin never hears about. The whole game is maximizing the fraction of traffic that terminates as far left in that chain as possible.

## Cache keys and normalization

The edge decides "have I seen this before?" by computing a **cache key**. By default the key is the request's identity: scheme, host, and path plus query string. Cloudflare's default key is the full URL (scheme + host + URI-with-query), plus a few CORS/method/forwarding headers. Query strings matter — `?foo=bar` and `?foo=baz` are two different objects by default.

That default is often wrong for real traffic, and normalization is where hit ratios are won or lost. Marketing UTM parameters (`?utm_source=...`) don't change the response but fragment the cache: a hundred campaign variants of one article become a hundred cold entries. So CDNs let you rewrite the key:

- **Query string include/exclude.** Cloudflare lets you `include` only meaningful params or `exclude: "*"` to drop the query string from the key entirely.
- **Selected headers.** Fold specific request headers into the key (e.g. `Accept-Encoding`, device type). Cloudflare restricts which headers are eligible and forbids `Cookie` in the key directly.
- **Host / cookie / device.** Normalize the host, key on a specific cookie, or split `mobile`/`desktop`/`tablet` variants.

The origin's own tool for this is the `Vary` response header — `Vary: Accept-Encoding` tells the cache to keep separate gzip and brotli copies. Over-broad `Vary` (e.g. `Vary: User-Agent`) shatters the cache into near-unique entries and is a classic self-inflicted MISS storm.

## Edge TTLs: overriding the origin

Origins frequently send caching headers meant for *browsers* (`Cache-Control: max-age=0, private`) while still wanting the CDN to cache aggressively. The mechanism that separates the two audiences is **`Surrogate-Control`**, a header aimed specifically at proxies and CDNs.

Fastly computes the edge TTL from `Surrogate-Control` in the same way it would from `Cache-Control`, but **prefers `Surrogate-Control` when both are present**, and strips it before the response reaches the client. So this response tells the browser not to cache while telling the edge to cache for a day and serve stale for a minute during revalidation:

```http
Cache-Control: max-age=0, private
Surrogate-Control: max-age=86400, stale-while-revalidate=60, stale-if-error=86400
```

The browser sees `Cache-Control: max-age=0`. The edge sees a 24-hour object. When these are absent, CDNs fall back to their own default TTL (Cloudflare's Edge Cache TTL, set via Cache Rules) — a CDN-specific TTL that has nothing to do with what the origin said.

## HIT, MISS, EXPIRED, and the Age header

CDNs expose their decision so you can debug it. Fastly returns `X-Cache: HIT`/`MISS` (and `HIT, MISS` across the edge/shield pair); Cloudflare returns `CF-Cache-Status: HIT | MISS | EXPIRED | REVALIDATED | DYNAMIC | BYPASS`. `EXPIRED` means the object was present but stale, so the edge revalidated with origin.

The `Age` header is the honest clock: how many seconds the object has sat in caches since it was fetched from origin. `Age: 3600` on a `max-age=86400` object means 23 hours of freshness remain. A response that keeps returning `Age: 0` is never actually caching — a MISS wearing a HIT's clothes.

## Tiered caching and origin shielding

Here is the CDN-scale stampede defense. Without shielding, every edge PoP that misses talks to origin independently — hundreds of PoPs, each a separate cold-cache client hammering your servers for the same object. **Shielding** designates one PoP as the funnel: as Fastly puts it, "visitor requests from across the global network funnel through a single, designated shield PoP" before reaching origin. The edge caches for users; the shield caches for edges. An edge MISS becomes a shield HIT far more often than an origin request.

Cloudflare's **Tiered Cache** is the same idea: lower-tier data centers (closest to visitors) query an upper tier, and "only the upper-tier can ask the origin for content." **Smart Tiered Cache** picks that upper tier automatically using latency data to find the data center best-connected to your origin. Akamai's equivalent is **Tiered Distribution** / cache parents; Varnish deployments build the shape by hand with an edge tier pointing at an origin-side Varnish tier.

The payoff: a viral object experiences at most one origin fetch per shield, not one per edge PoP. Origin load is bounded by the number of shields, not the number of PoPs or users.

## Request collapsing (coalescing)

Within a single PoP, the second defense is **request collapsing**. When many requests for the same key arrive during a MISS, only the first goes to the backend; the rest join a waiting list and are all served from the single fetched response. Fastly enables this by default for cacheable misses in both VCL and Compute services. Combined with clustering inside a PoP and the shield tier, Fastly notes there are "up to four opportunities" to collapse a request before it reaches origin.

The dangerous corner is uncacheable content: if the response turns out to be `private`, waiting requests can't share it and must proceed one at a time — turning a queue into serialized multi-second latency. The fix is a **hit-for-pass** object: cache the *decision* "don't cache this" for a short window so subsequent requests skip the queue and fetch concurrently. In Varnish/VCL this is `beresp.uncacheable` in `vcl_backend_response`; the concept exists precisely so coalescing doesn't backfire on dynamic responses.

## Purge and invalidation

Caching is easy; invalidating is the hard half. CDNs offer three granularities:

1. **Single-URL purge** — evict exactly `https://ex.com/article/42`. Precise, but you must know every URL.
2. **Wildcard / path purge** — evict `https://ex.com/blog/*`. Coarse and often slower.
3. **Surrogate-key / cache-tag purge** — the powerful one. The origin tags each response with a `Surrogate-Key` header (Cloudflare calls it `Cache-Tag`), listing logical groups the object belongs to. Purging a key evicts every object carrying it, everywhere, at once.

Concretely: a product page and a category page both embed product 812. Origin tags each response:

```http
# response for /product/812
Surrogate-Key: product-812 category-shoes brand-acme

# response for /category/shoes
Surrogate-Key: category-shoes
```

Keys are space-separated and many-to-many — one object carries several keys, one key spans many objects. When product 812's price changes, one API call purges everything referencing it, without you enumerating URLs:

```bash
curl -X POST https://api.fastly.com/service/$SERVICE/purge/product-812 \
  -H "Fastly-Key: $TOKEN"
```

Both `/product/812` and any other page tagged `product-812` go stale instantly; the category page tagged only `category-shoes` stays cached. This is how large sites keep TTLs high (great hit ratios) while still reflecting edits in seconds.

## Dynamic vs static content

Not everything is a cacheable blob. The edge has escape hatches for the rest:

- **ESI (Edge Side Includes).** Cache the page shell for hours, mark a fragment `<esi:include src="/cart-summary"/>`, and let the edge assemble personalized bits per request. Supported in Fastly VCL and Varnish (`esi` in `vcl_backend_response`).
- **stale-while-revalidate at the edge.** Serve the stale copy instantly and revalidate in the background so no user waits on origin; pair with `stale-if-error` to ride out origin outages.
- **Compute@edge.** Fastly Compute (WebAssembly) and Cloudflare Workers run code in the PoP — computing cache keys, doing auth, synthesizing responses — turning the CDN into a programmable layer in front of your cache.

The through-line: a modern CDN is not a dumb mirror. It is a distributed, programmable cache designed to answer as far from your origin as possible — and to invalidate precisely enough that you can afford to.

**Try next:** Add `Surrogate-Key` headers to two related endpoints in a test service, cache them with a long `Surrogate-Control: max-age`, then watch `Age` climb on repeated requests and drop to zero after a single-key purge — and compare `CF-Cache-Status` / `X-Cache` before and after enabling shielding or Tiered Cache.

## Hit ratio math

Cache hit ratio = hits / (hits + misses). The number that matters is its complement: **origin load = (1 − hit ratio) × request rate**. At 10,000 rps, 90% → 1,000 rps to origin; 99% → 100 rps. Moving from 98% to 99% *halves* origin traffic — which is why origin shield exists, why you normalize cache keys (strip marketing query params, limit `Vary`), and why a purge-all is so violent: it takes you to 0% instantly against an origin provisioned for 1%.
