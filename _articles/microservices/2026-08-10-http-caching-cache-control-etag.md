---
title: "HTTP Caching: Cache-Control, ETags, and the 304 Dance"
date: 2026-08-10
track: microservices
summary: The web ships with a caching layer built into the protocol, and interviewers expect you to know it cold. Freshness vs revalidation, the precise difference between no-cache, no-store, and must-revalidate, conditional requests with ETag and Last-Modified, Vary and cache keys, and the stale-while-revalidate extensions from RFC 5861.
reading_time: 7
tags:
  - caching
  - http
  - performance
  - cdn
  - web
sources:
  - title: "RFC 9111 — HTTP Caching"
    url: "https://www.rfc-editor.org/rfc/rfc9111.html"
  - title: "RFC 5861 — HTTP Cache-Control Extensions for Stale Content"
    url: "https://datatracker.ietf.org/doc/html/rfc5861"
  - title: "HTTP caching (MDN Web Docs)"
    url: "https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Caching"
  - title: "Cache-Control header (MDN Web Docs)"
    url: "https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Cache-Control"
  - title: "Prevent unnecessary network requests with the HTTP Cache (web.dev)"
    url: "https://web.dev/articles/http-cache"
---

Every backend engineer reaches for Redis or a CDN eventually, but the most widely deployed cache in the world is the one baked into HTTP itself. Every browser, every reverse proxy, every CDN edge node speaks it. Get the response headers right and a request never leaves the client; get them slightly wrong and you either serve stale data to millions of users or hammer your origin with traffic you already answered. This article is about the protocol semantics defined in **RFC 9111** — not CDN internals, which are their own topic — with the interview-grade precision that trips people up.

## Two kinds of cache

The spec draws a hard line between **private** and **shared** caches, and almost every directive means something different depending on which one you are talking to.

- A **private cache** belongs to a single user — the browser's on-disk cache is the canonical example. It may store personalized responses.
- A **shared cache** sits between many users and the origin: a forward proxy, a reverse proxy, a CDN. It must never leak one user's response to another.

The `private` directive means "a shared cache MUST NOT store this response" (RFC 9111 §5.2.2.7) — it is for a single user. Use it on anything personalized: account pages, responses that vary by cookie. The `public` directive is the opposite escape hatch: "a cache MAY store the response even if it would otherwise be prohibited" (§5.2.2.9), which matters mainly when an `Authorization` header is present, since authenticated responses are not stored by shared caches by default.

## Freshness: serving without asking

The core idea is the **freshness lifetime**. While a stored response is fresh, a cache serves it directly — zero network round-trips to the origin. A response is fresh while:

```
freshness_lifetime > current_age
```

You set the lifetime with `Cache-Control: max-age=N`, in seconds. Per §5.2.2.1, the response "is to be considered stale after its age is greater than" N.

```http
Cache-Control: max-age=3600
```

`s-maxage` does the same thing but **only for shared caches**, overriding `max-age` for them (§5.2.2.10). This lets you keep a short browser lifetime and a long CDN lifetime from one response:

```http
Cache-Control: max-age=60, s-maxage=86400
```

Browsers treat this as fresh for 60 seconds; the CDN holds it for a day.

The legacy `Expires` header names an absolute date instead of a duration. When both are present, `max-age` wins. `Expires` survives mostly for HTTP/1.0 compatibility and is vulnerable to client clock skew, which is exactly why `max-age` (a relative delta) replaced it.

### The Age header

Shared caches advertise how long they have been holding a response with `Age`, "the cache's estimate of the number of seconds since the origin server generated or validated the response" (§4.2.3). A client computes remaining freshness as `max-age - Age`:

```http
HTTP/1.1 200 OK
Date: Mon, 10 Aug 2026 12:00:00 GMT
Cache-Control: max-age=604800
Age: 86400
```

604800 − 86400 = 518400 seconds of freshness left. A stubbornly high `Age` on a resource you expected to be fresh is the classic sign of a CDN serving an old copy.

### Heuristic freshness

What if a response has no `max-age` and no `Expires`? Caches are allowed to guess. §4.2.2 permits assigning a **heuristic** expiration time from other fields, most commonly `Last-Modified`. The widespread convention: cache for **10% of the time since the resource was last modified**. A file last changed 100 days ago gets treated as fresh for ~10 days. This is why a response with no cache headers at all can still be cached in surprising ways — always set `Cache-Control` explicitly rather than relying on heuristics.

## Revalidation: asking cheaply

Once a response goes stale, the cache does not throw it away. It **revalidates** — asks the origin "is my copy still good?" using a conditional request. If nothing changed, the origin replies `304 Not Modified` with no body, and the cache refreshes its copy without re-downloading the payload. Two validators drive this.

**ETag / If-None-Match** — the server stamps the response with an opaque token (a hash, a version):

```http
ETag: "33a64df5c7"
```

On revalidation the client echoes it back:

```http
If-None-Match: "33a64df5c7"
```

**Last-Modified / If-Modified-Since** — the server sends a timestamp; the client sends it back in `If-Modified-Since`. ETags are stronger: they are immune to clock skew and detect changes that revert to the same timestamp or happen within a one-second window. When both are present, `If-None-Match` takes precedence over `If-Modified-Since`.

## The worked flow: 200 → 304

First request, cold cache:

```http
GET /api/products/42 HTTP/1.1
Host: shop.example.com
```

```http
HTTP/1.1 200 OK
Date: Mon, 10 Aug 2026 12:00:00 GMT
Cache-Control: max-age=60
ETag: "v7-9f2a"
Content-Length: 1843

{ "id": 42, "name": "...", ... }
```

The client stores it, fresh for 60 seconds. Within that window, reads are served locally — no request at all. At 12:01:30, 90 seconds later, the copy is stale. The client does not blindly refetch; it revalidates:

```http
GET /api/products/42 HTTP/1.1
Host: shop.example.com
If-None-Match: "v7-9f2a"
```

The product has not changed, so:

```http
HTTP/1.1 304 Not Modified
Date: Mon, 10 Aug 2026 12:01:30 GMT
Cache-Control: max-age=60
ETag: "v7-9f2a"
```

No body. The 1843-byte payload was not re-sent; the cache marks its stored copy fresh again for another 60 seconds. If the product *had* changed, the origin would instead return `200 OK` with a new ETag and the full body.

## no-cache vs no-store vs must-revalidate

This is the trio interviewers use to separate people who memorized names from people who understand the model. The names are misleading, so anchor on the RFC.

- **`no-cache`** — *store, but always revalidate before reuse.* Per §5.2.2.4, the response "MUST NOT be used to satisfy any other request without forwarding it for validation." It does **not** forbid caching. It means: keep the copy, but on every use send a conditional request. Combined with ETags this is cheap — you get a 304 most of the time. Ideal for HTML that changes unpredictably but is often unchanged.
- **`no-store`** — *never write it down.* §5.2.2.5: "a cache MUST NOT store any part of either the immediate request or the response." Nothing is retained. This is for genuinely sensitive data — banking details, one-time tokens. It is the only directive that actually prevents storage.
- **`must-revalidate`** — *fresh is fine; once stale, no serving without validation.* §5.2.2.2: "once the response has become stale, a cache MUST NOT reuse that response... until it has been successfully validated." The key difference from `no-cache`: while the response is still fresh, `must-revalidate` serves it directly with no origin contact. It only bites after expiry. Its real purpose is disabling the spec's allowance to serve stale content on errors or disconnection — with `must-revalidate`, a cache that cannot reach the origin must return `504`, not a stale copy.

So: `no-cache` revalidates on every use; `must-revalidate` serves freely until stale, then forces validation; `no-store` opts out of caching entirely. The common `Cache-Control: no-cache` on HTML is *not* "don't cache" — that is `no-store`.

`immutable` is the mirror image: `Cache-Control: max-age=31536000, immutable` tells the browser the content will never change, so it should not even revalidate on a manual reload. Reserve it for fingerprinted assets like `app.4f2c8a.js`, whose URL changes when content does.

## Vary and the cache key

A cache keys entries by URL — but a single URL can have multiple representations (gzip vs brotli, English vs Japanese). `Vary` extends the cache key to include named request headers:

```http
Vary: Accept-Encoding, Accept-Language
```

Now the cache stores a separate entry per encoding/language combination, and only returns one to a request whose headers match. The trap is `Vary: User-Agent` (or anything high-cardinality): it fragments the cache into near-unique entries and destroys the hit rate. Vary only on headers you genuinely branch on.

## Serving stale on purpose: RFC 5861

Two extensions let you trade a little staleness for a lot of resilience.

**`stale-while-revalidate=N`** — after a response goes stale, for N seconds the cache may serve the stale copy **immediately** while revalidating in the background, "without blocking." The user never eats the revalidation latency; the next user gets the fresh copy.

```http
Cache-Control: max-age=600, stale-while-revalidate=30
```

**`stale-if-error=N`** — if the origin returns a 5xx (or is unreachable) within N seconds of staleness, the cache serves the stale copy instead of the error. It is a protocol-level circuit breaker; such responses stay "visibly stale" with a non-zero `Age`.

```http
Cache-Control: max-age=600, stale-if-error=86400
```

Together they turn a hard TTL into a soft one: fast for users, forgiving of a flaky origin.

**Try next:** trace how a CDN edge node layers on top of these semantics — request collapsing, tiered caches, and cache-key normalization — in the companion article on CDN and edge caching internals.
