---
title: "CDN & Edge Caching: PoPs, Cache-Control, Surrogate Keys, and the Art of the Purge"
date: 2026-08-13
track: microservices
summary: "How a CDN actually routes and layers its caches (anycast, PoPs, origin shield), which response headers control what gets cached where and for how long, and why hard-purging everything is how you take your own origin down. With real headers and a curl demonstration."
reading_time: 6
tags: [cdn, caching, cache-control, edge, system-design]
sources:
  - title: "What is an Anycast Network? (Cloudflare Learning Center)"
    url: "https://www.cloudflare.com/learning/cdn/glossary/anycast-network/"
  - title: "Using Amazon CloudFront Origin Shield (AWS documentation)"
    url: "https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/origin-shield.html"
  - title: "Soft purges (Fastly documentation)"
    url: "https://www.fastly.com/documentation/guides/full-site-delivery/purging/soft-purges/"
  - title: "Purging with surrogate keys (Fastly documentation)"
    url: "https://www.fastly.com/documentation/guides/full-site-delivery/purging/purging-with-surrogate-keys/"
  - title: "RFC 5861 — HTTP Cache-Control Extensions for Stale Content"
    url: "https://datatracker.ietf.org/doc/html/rfc5861"
---

"How would you serve this globally?" is where system design interviews go after you've scaled the database. The expected answer is a CDN — but the follow-ups ("how does the user reach the nearest node?", "how do you invalidate?") separate people who've configured one from people who've drawn a cloud labeled "CDN."

## How requests find the edge

A CDN is thousands of caches in **PoPs** (points of presence) near users. Two routing schemes get a request to the right one:

- **DNS-based routing**: your hostname CNAMEs to the CDN; its DNS returns different IPs depending on the resolver's location (CloudFront's classic model).
- **Anycast**: every PoP announces the *same* IP via BGP, and internet routing delivers each packet to the topologically nearest PoP (Cloudflare, Fastly). No DNS trickery, and traffic spikes or DDoS load spread across the whole fleet.

A miss at the edge doesn't necessarily hit your origin. Modern CDNs are **cache hierarchies**: edge PoP → regional/shield cache → origin. CloudFront's **Origin Shield** is an explicit extra layer that all regional caches funnel through, with **request collapsing**: concurrent misses for the same object are consolidated into "as few as one request going to your origin" (AWS docs). One layer of shield can turn a thousand simultaneous global misses into a single origin fetch — the same request-coalescing idea covered in the cache-stampede article here, run by someone else's fleet.

## The headers that drive everything

```http
HTTP/2 200
cache-control: public, max-age=60, s-maxage=3600, stale-while-revalidate=300, stale-if-error=86400
surrogate-key: product-42 catalog pricing-v3
etag: "5f3a2b"
```

- `max-age=60` — browsers may cache for 60 s.
- `s-maxage=3600` — *shared* caches (the CDN) may cache for an hour; overrides `max-age` there. Splitting the two lets you purge the CDN centrally while browsers only hold content briefly.
- `stale-while-revalidate=300` (RFC 5861) — after expiry, serve the stale copy for up to 5 min while refetching in the background. Users never wait on origin latency.
- `stale-if-error=86400` — if origin is down or 5xx-ing, keep serving stale for a day. Free resilience.
- `Surrogate-Key` — Fastly's tag header (Cloudflare calls them Cache-Tags): label every response with the entities it depends on, so you can later purge "everything mentioning product-42" in one API call instead of enumerating URLs.

For assets with hashed filenames (`app.3f9c1d.js`), go maximal: `cache-control: public, max-age=31536000, immutable` — the URL changes when the content does, so invalidation is never needed.

## Invalidation, purge, soft purge

Three ways cached content dies, in decreasing order of violence:

| | Mechanism | Origin impact | Use for |
|---|---|---|---|
| TTL expiry | `s-maxage` runs out | Smooth, spread over time | Default; most content |
| Hard purge | Object dropped immediately | Next request = guaranteed miss | Legal/security removals |
| Soft purge | Object marked *stale*, served while revalidating | One background refresh per object | Content updates |

Hard purge is the dangerous one. Purge a popular object — or worse, purge-all — and every PoP misses simultaneously: a self-inflicted **thundering herd** that can flatten an origin sized for a 95%+ hit ratio. Fastly's soft purge instead marks objects stale so `stale-while-revalidate` and `stale-if-error` take over; notably, Fastly's purge-all *cannot* be soft, which is why their docs suggest a constant surrogate key on everything as a soft-purgeable "all."

```bash
# Inspect cache behavior: look for hit/miss and object age
curl -sI https://www.fastly.com/ | grep -iE 'x-cache|age|cache-control'
# x-cache: HIT        <- served from a PoP
# age: 217            <- seconds since the CDN fetched it

# Soft-purge one URL (Fastly)
curl -X PURGE -H "Fastly-Soft-Purge: 1" https://www.example.com/products/42

# Soft-purge by tag: every page carrying surrogate-key "product-42"
curl -X POST -H "Fastly-Key: $TOKEN" -H "Fastly-Soft-Purge: 1" \
  "https://api.fastly.com/service/$SERVICE_ID/purge/product-42"
```

Run the `curl -sI` twice: the first response may show `x-cache: MISS`, the second `HIT` with a small `age`. That two-line demo is worth more in an interview than any diagram.

## Hit ratio math

Cache hit ratio = hits / (hits + misses). The number that matters is its complement: **origin load = (1 − hit ratio) × request rate**. At 10,000 rps, 90% → 1,000 rps to origin; 99% → 100 rps. Moving from 98% to 99% *halves* origin traffic — which is why origin shield exists, why you normalize cache keys (strip marketing query params, limit `Vary`), and why a purge-all is so violent: it takes you to 0% instantly against an origin provisioned for 1%.

## What to cache

- **Static assets**: hashed filenames, `immutable`, one-year TTL. No purging ever.
- **HTML / rendered pages**: short `s-maxage` (30–300 s) + `stale-while-revalidate`, purge by surrogate key on publish. Even 10 s absorbs a front-page spike.
- **API responses**: cache anonymous, read-heavy `GET`s (catalogs, search, config) at the edge with `s-maxage` + tags. Never cache per-user responses under a shared key — that's how you leak one user's data to another; mark them `cache-control: private, no-store`.
- **Gated content**: don't put auth at origin only — use **signed URLs / signed cookies**: origin signs an expiring token into the URL, the CDN verifies the signature at the edge and still serves from cache. Standard for video segments and paid downloads (CloudFront and Fastly both support it).

**Try next:** Run `curl -sI` twice against your own production site and one page of a big storefront; compare `cache-control`, `age`, and hit/miss headers, and work out what a purge-all at your measured hit ratio would multiply your origin traffic by.
