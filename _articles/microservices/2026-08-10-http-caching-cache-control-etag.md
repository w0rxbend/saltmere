---
title: "HTTP Caching: Cache-Control, ETags, and the 304 Exchange"
date: 2026-08-10
track: microservices
summary: The caching layer built into HTTP itself. Freshness versus revalidation, the precise difference between no-cache, no-store, and must-revalidate, conditional requests with ETag and Last-Modified, Vary and the cache key, and the stale-content extensions of RFC 5861.
reading_time: 8
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

**Gist.** Repeated requests for unchanged representations waste both latency and origin capacity, and the most widely deployed cache addressing that is the one specified by the protocol itself: every browser, forward proxy, reverse proxy and content delivery network (CDN) edge implements it. **RFC 9111** defines two mechanisms — a *freshness lifetime* during which a stored response is reused with no network round-trip, and *revalidation*, a conditional request that returns `304 Not Modified` with no body when the stored copy is still valid. The cost is a correctness obligation on the response headers: a lifetime that is too long serves stale representations for its full duration with no way to recall them, and a cache key that omits a header on which the response varies serves one user's representation to another.

This article covers the protocol semantics of RFC 9111 and RFC 5861, not CDN internals.

## Two classes of cache

The specification separates **private** from **shared** caches, and most directives are defined relative to that distinction.

- A **private cache** serves a single user; the browser's on-disk cache is the canonical instance. It may store personalized responses.
- A **shared cache** sits between many users and the origin: a forward proxy, a reverse proxy, a CDN node. It must not return one user's response to another.

The `private` directive states that **a shared cache must not store the response** (RFC 9111 §5.2.2.7); it marks the response as intended for a single user, and applies to account pages and anything that branches on a session cookie. The `public` directive states that **a cache may store the response even where storage would otherwise be prohibited** (§5.2.2.9); its principal effect concerns requests carrying an `Authorization` header, which shared caches do not store by default.

## Freshness: reuse without contacting the origin

A stored response is reused directly, with **zero origin round-trips**, while it is fresh. The predicate is:

```
freshness_lifetime > current_age
```

`Cache-Control: max-age=N` sets the lifetime in seconds. Per §5.2.2.1 the response **is to be considered stale once its age exceeds N**.

```http
Cache-Control: max-age=3600
```

`s-maxage` sets the same lifetime **for shared caches only**, overriding `max-age` for them (§5.2.2.10). A single response can therefore carry a short private lifetime and a long shared one:

```http
Cache-Control: max-age=60, s-maxage=86400
```

Browsers hold this fresh for 60 seconds; a shared cache holds it for 86400 seconds.

The `Expires` header names an absolute date rather than a duration. **When both are present, `max-age` takes precedence.** An absolute date is evaluated against the client's clock and is therefore sensitive to clock skew, whereas `max-age` is a relative delta.

### The Age header

A shared cache reports how long it has held a response in `Age`, defined as **the sender's estimate of the time in seconds since the response was generated or successfully validated at the origin** (§5.1); §4.2.3 gives the algorithm that computes it. Remaining freshness at the client is `max-age − Age`:

```http
HTTP/1.1 200 OK
Date: Mon, 10 Aug 2026 12:00:00 GMT
Cache-Control: max-age=604800
Age: 86400
```

604800 − 86400 = 518400 seconds of freshness remain. A persistently high `Age` on a resource expected to be fresh indicates an intermediate cache holding an old copy.

### Heuristic freshness

A response carrying neither `max-age` nor `Expires` is not thereby uncacheable. §4.2.2 permits a cache to assign a **heuristic** expiration time derived from other fields, most commonly `Last-Modified`. The widespread convention assigns **10% of the elapsed time since the last modification**: a representation last changed 100 days ago is treated as fresh for roughly 10 days. Omitting `Cache-Control` therefore does not disable caching; it delegates the lifetime to the cache's heuristic.

## Revalidation: confirming cheaply

A stale response is not discarded. The cache **revalidates** it with a conditional request; if the representation is unchanged the origin returns **`304 Not Modified` with no body**, and the cache marks its stored copy fresh again without retransmitting the payload. Two validators exist.

**ETag / If-None-Match.** The origin stamps the response with an opaque token — a hash, a version counter, any value it can recompute:

```http
ETag: "33a64df5c7"
```

The cache echoes it on revalidation:

```http
If-None-Match: "33a64df5c7"
```

**Last-Modified / If-Modified-Since.** The origin sends a timestamp and the cache returns it in `If-Modified-Since`. The timestamp has **one-second resolution**, so two modifications within the same second are indistinguishable, and a modification that restores a previous timestamp is likewise invisible; an entity tag has neither limitation and does not depend on clock agreement. **When both validators are present in a request, `If-None-Match` is evaluated and `If-Modified-Since` ignored.**

## Worked exchange: 200 then 304

First request against a cold cache:

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

The response is stored and fresh for 60 seconds; reads inside that window issue no request at all. At 12:01:30, 90 seconds later, the copy is stale, and the cache revalidates rather than refetching:

```http
GET /api/products/42 HTTP/1.1
Host: shop.example.com
If-None-Match: "v7-9f2a"
```

The representation is unchanged:

```http
HTTP/1.1 304 Not Modified
Date: Mon, 10 Aug 2026 12:01:30 GMT
Cache-Control: max-age=60
ETag: "v7-9f2a"
```

**The 1843-byte body is not retransmitted**, and the stored copy becomes fresh for a further 60 seconds. Had the representation changed, the origin would have returned `200 OK` with a new entity tag and the full body.

## no-cache, no-store, must-revalidate

The three names do not describe their behaviour, so each is stated from the specification.

- **`no-cache`** — *store, but validate before every reuse.* Per §5.2.2.4 the response **must not be used to satisfy another request without forwarding it for validation**. Storage is not prohibited. Paired with an entity tag the cost per reuse is one conditional request that usually returns a bodiless 304, which suits HTML that changes unpredictably but is frequently unchanged.
- **`no-store`** — *do not retain.* §5.2.2.5: **a cache must not store any part of either the request or any response to it.** This is the only directive that prevents storage, and it is the one applicable to credentials and one-time tokens.
- **`must-revalidate`** — *fresh reuse is permitted; stale reuse is not.* §5.2.2.2: once the response has become stale, **a cache must not reuse it until it has been successfully validated**. While the response remains fresh it is served with no origin contact, so the directive binds only after expiry. Its distinguishing effect is on the specification's allowance to serve a stale response when the origin cannot be reached: under `must-revalidate` a cache unable to validate must respond `504 (Gateway Timeout)` rather than serve the stale copy.

Summarised: `no-cache` validates on every reuse, `must-revalidate` reuses freely until stale and then requires validation, `no-store` opts out of storage. `Cache-Control: no-cache` on an HTML document does not mean "do not cache"; that is `no-store`.

`immutable` occupies the opposite position. `Cache-Control: max-age=31536000, immutable` signals that the representation will not change, and a browser therefore need not revalidate even on an explicit reload. It applies to fingerprinted assets such as `app.4f2c8a.js`, whose URL changes whenever the content does.

## Vary and the cache key

The cache key is derived from the request URL, but one URL can have several representations — gzip against brotli, English against Japanese. `Vary` extends the key with the named request headers:

```http
Vary: Accept-Encoding, Accept-Language
```

The cache then holds one entry per distinct combination of those header values and returns an entry only to a request whose values match. **A high-cardinality header such as `User-Agent` in `Vary` partitions the cache into near-unique entries and collapses the hit rate**, since each distinct user agent string forms its own key.

## Deliberate staleness: RFC 5861

RFC 5861 defines two extension directives that exchange bounded staleness for availability.

**`stale-while-revalidate=N`.** For N seconds after the response becomes stale, a cache may **serve the stale copy immediately while revalidating in the background**, without blocking the request on the revalidation. The requesting client does not pay the revalidation latency; a later client receives the refreshed copy.

```http
Cache-Control: max-age=600, stale-while-revalidate=30
```

**`stale-if-error=N`.** If revalidation encounters an error — a 5xx status or an unreachable origin — within N seconds of the response becoming stale, the cache may serve the stale copy in place of the error. The copy is served as it was stored, so its `Age` continues to reflect the time since the origin generated or last validated it.

```http
Cache-Control: max-age=600, stale-if-error=86400
```

### Implementation sketch (Scala)

The freshness and revalidation decision at a cache, expressed over the directives above. `swr` and `sie` hold the RFC 5861 values.

```scala
final case class Stored(
    body: Array[Byte],
    etag: Option[String],
    maxAge: Long,           // seconds, from Cache-Control
    storedAt: Long,         // epoch seconds
    noCache: Boolean,
    mustRevalidate: Boolean,
    swr: Long,
    sie: Long
)

enum Action:
  case Serve(entry: Stored)
  case ServeAndRefresh(entry: Stored)          // stale-while-revalidate
  case Revalidate(ifNoneMatch: Option[String]) // blocking conditional request

def decide(e: Stored, now: Long): Action =
  val age = now - e.storedAt
  if e.noCache then Action.Revalidate(e.etag)
  else if age <= e.maxAge then Action.Serve(e)
  // must-revalidate forbids any stale reuse, so it also suppresses stale-while-revalidate
  else if !e.mustRevalidate && age <= e.maxAge + e.swr then Action.ServeAndRefresh(e)
  else Action.Revalidate(e.etag)

/** Applied to the outcome of a blocking revalidation. */
def onRevalidation(e: Stored, status: Int, now: Long): Option[Stored] =
  status match
    case 304 => Some(e.copy(storedAt = now))        // refreshed, body reused
    case s if s >= 500 && !e.mustRevalidate
          && now - e.storedAt <= e.maxAge + e.sie => Some(e)  // stale-if-error
    case _ => None                                  // propagate origin response
```

## Pitfalls

- `Cache-Control: no-cache` is read as prohibiting storage; it permits storage and requires validation before each reuse, so credentials sent under it remain on disk. Only `no-store` prevents retention.
- A response with no `Cache-Control` and no `Expires` is assumed uncached; §4.2.2 permits heuristic expiration from `Last-Modified`, commonly 10% of the elapsed time since modification, so an old resource can be held fresh for days.
- A representation published with a long `max-age` cannot be recalled: caches holding it will not contact the origin until it expires. Content changes must be published under a new URL, which is what fingerprinted asset names achieve.
- `Vary: User-Agent` yields a near-zero hit rate at shared caches because each distinct user agent string forms a separate cache key.
- A personalized response without `private` is stored by shared caches and returned to other users; the URL alone is the key unless `Vary` names the distinguishing header.
- `Expires` is compared against the client's clock, so a skewed client treats the response as already expired or as fresh past its intended lifetime; `max-age` is unaffected because it is a delta.
- `s-maxage` overrides `max-age` at shared caches only, so a short `max-age` intended to bound staleness has no effect at a CDN that observes the longer `s-maxage`.
- `must-revalidate` converts an unreachable origin into `504` once the stored response is stale, removing the fallback to stale content that a cache would otherwise be permitted to serve.
