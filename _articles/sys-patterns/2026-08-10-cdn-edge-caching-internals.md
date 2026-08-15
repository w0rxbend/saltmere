---
title: 'CDN & Edge Caching Internals: The Request Path from PoP to Origin'
date: 2026-08-10
track: sys-patterns
summary: The request path a content delivery network takes through edge points of presence, mid-tier shields, and origin; cache-key construction and header normalization; Surrogate-Control versus Cache-Control precedence; HIT/MISS/EXPIRED status and the Age header; tiered caching and origin shielding as a stampede defence at CDN scale; request collapsing and hit-for-pass; surrogate-key and cache-tag purge; and the dynamic-content escape hatches (ESI, stale-while-revalidate, compute at the edge). With Fastly, Cloudflare, Akamai, and Varnish/VCL examples.
reading_time: 8
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

**Gist.** A single origin cannot answer every request in the world at acceptable latency, and a flat fleet of independent edge caches converts one cold object into one origin fetch per cache. A content delivery network (CDN) addresses both with a layered topology — edge point of presence (PoP), mid-tier shield, origin — plus a cache key that decides object identity and a purge mechanism that invalidates by logical tag rather than by URL. The cost is that correctness now depends on key construction and invalidation reach: an over-broad key fragments the cache into near-unique entries, and a tag that fails to cover an object leaves stale content served for the full time-to-live (TTL).

The protocol-level rules — `Cache-Control`, `ETag`, freshness, revalidation — are covered in [HTTP caching semantics](/articles/microservices/2026-08-10-http-caching-cache-control-etag), and the load-shedding arithmetic behind coalescing in [cache stampede & request coalescing](/articles/microservices/2026-08-10-cache-stampede-request-coalescing).

## The request path

A client resolves the hostname to an anycast address — one Internet Protocol (IP) address announced from many locations, with Border Gateway Protocol (BGP) routing each client to a topologically near announcement — and reaches a nearby **edge PoP**. On a **HIT** in the local cache, the object is returned from that PoP and no further hop occurs.

On a **MISS**, the edge does not necessarily contact origin. In a tiered topology it forwards to a **mid-tier or shield PoP**, chosen for proximity to the origin. Only when the shield also misses does a request traverse the public internet to the origin:

```
client → edge PoP → (shield / upper-tier PoP) → origin
```

**Every hop is itself a cache, so each layer that answers is a layer the origin never observes.** The design objective is to maximize the fraction of traffic terminating as early in that chain as possible.

## Cache keys and normalization

The edge determines object identity by computing a **cache key**. Cloudflare's default key is the full uniform resource locator (URL) — scheme, host, and uniform resource identifier (URI) including query string — together with a small set of cross-origin resource sharing (CORS), method, and forwarding headers. **Because the query string participates by default, `?foo=bar` and `?foo=baz` are distinct objects.**

Urchin Tracking Module (UTM) marketing parameters such as `?utm_source=...` do not alter the response body but do alter the key, so *n* campaign variants of one document occupy *n* cold entries. CDNs therefore expose key rewriting:

- **Query-string include/exclude.** Cloudflare supports listing only the parameters that affect the response with `include`, or `exclude: "*"` to remove the query string from the key entirely.
- **Selected headers.** Specific request headers (for example `Accept-Encoding`, or a device-type header) may be folded into the key. Cloudflare restricts which headers are eligible and **forbids `Cookie` as a key header directly**.
- **Host, cookie, and device.** The host may be normalized, a named cookie keyed on, and `mobile`/`desktop`/`tablet` variants separated.

The origin's own instrument is the `Vary` response header: `Vary: Accept-Encoding` instructs caches to retain separate gzip and Brotli representations. **`Vary: User-Agent` partitions the cache along a near-unique axis**, since user-agent strings vary per browser build, and produces a sustained MISS rate.

## Edge TTLs: overriding the origin

Origins commonly emit caching headers intended for browsers (`Cache-Control: max-age=0, private`) while still requiring aggressive CDN caching. **`Surrogate-Control` is the header addressed to proxies and CDNs rather than to user agents**, which separates the two audiences.

Fastly computes the edge TTL from `Surrogate-Control` using the same rules it applies to `Cache-Control`, **prefers `Surrogate-Control` when both headers are present, and strips it from the response before it reaches the client**. The following pair instructs the browser not to cache while granting the edge a one-day object with a one-minute stale-while-revalidate window:

```http
Cache-Control: max-age=0, private
Surrogate-Control: max-age=86400, stale-while-revalidate=60, stale-if-error=86400
```

`stale-while-revalidate` and `stale-if-error` are defined in RFC 5861. When neither caching header is present, the CDN applies its own default TTL — Cloudflare's Edge Cache TTL, configured through Cache Rules — unrelated to anything the origin stated.

## HIT, MISS, EXPIRED, and the Age header

Fastly returns `X-Cache: HIT` or `MISS`, and `HIT, MISS` for the edge/shield pair, recording the outcome at each tier. Cloudflare returns `CF-Cache-Status` with values including `HIT`, `MISS`, `EXPIRED`, `REVALIDATED`, `DYNAMIC`, and `BYPASS`. **`EXPIRED` denotes an object that was present but stale, prompting revalidation against origin** — distinct from `MISS`, where no copy existed.

The `Age` header reports how many seconds the object has resided in caches since it was fetched from origin. `Age: 3600` on a `max-age=86400` object leaves 23 hours of freshness. **A response whose `Age` is persistently 0 is not being cached**, whatever the status header claims.

## Tiered caching and origin shielding

Without a shield tier, every edge PoP that misses contacts origin independently, so fan-out scales with the number of PoPs. **Shielding designates one PoP as a funnel**: Fastly documents that "visitor requests from across the global network funnel through a single, designated shield PoP" before reaching origin. The edge caches for users; the shield caches for edges.

Cloudflare's **Tiered Cache** implements the same shape: lower-tier data centres query an upper tier, and "only the upper-tier can ask the origin for content." **Smart Tiered Cache** selects that upper tier automatically using latency data to identify the data centre best connected to the origin. Akamai's counterpart is Tiered Distribution with cache parents; Varnish deployments construct the topology explicitly, with an edge tier using an origin-side Varnish tier as its backend.

The invariant that follows: **for a given object, origin fetches are bounded by the number of shields rather than the number of edge PoPs or clients.**

## Request collapsing (coalescing)

Within a single PoP the second defence is **request collapsing**. When several requests for the same key arrive while a MISS is outstanding, only the first is forwarded to the backend; the remainder wait and are served from that one fetched response. **Fastly enables collapsing by default for cacheable misses in both Varnish Configuration Language (VCL) and Compute services.** Combined with clustering inside a PoP and the shield tier, a request can meet collapsing at more than one point before it reaches origin: at the edge PoP and again at the shield.

The failure mode is uncacheable content. **If the fetched response is `private` or otherwise uncacheable, the waiting requests cannot share it and must be issued serially**, so a queue of *n* waiters becomes *n* sequential backend round trips. The remedy is a **hit-for-pass** object: cache the decision "this key is not cacheable" for a short interval so subsequent requests bypass the queue and fetch concurrently. VCL expresses it by marking the backend response uncacheable — `set beresp.uncacheable = true;` in Varnish's `vcl_backend_response` — which stores a short-lived hit-for-pass entry instead of the object.

### Implementation sketch (Scala)

A single-flight map is the load-bearing part of collapsing: one in-flight fetch per key, with a short-lived negative decision recorded when the result proves uncacheable.

```scala
import java.util.concurrent.ConcurrentHashMap
import scala.concurrent.{Future, ExecutionContext}

final case class Fetched(body: Array[Byte], cacheable: Boolean)

final class Collapser(fetch: String => Future[Fetched], hitForPassMillis: Long)(using ec: ExecutionContext):
  private val inFlight = ConcurrentHashMap[String, Future[Fetched]]()
  private val hitForPass = ConcurrentHashMap[String, Long]()

  def get(key: String): Future[Fetched] =
    if hitForPass.getOrDefault(key, 0L) > System.currentTimeMillis() then
      fetch(key) // known uncacheable: bypass the queue, fetch concurrently
    else
      var started = false
      // computeIfAbsent runs the mapping function under the bin lock, so exactly
      // one caller starts the backend request for this key; later callers join it.
      val f = inFlight.computeIfAbsent(key, k => { started = true; fetch(k) })
      if started then
        f.onComplete { result =>
          inFlight.remove(key, f)
          if result.toOption.exists(!_.cacheable) then
            hitForPass.put(key, System.currentTimeMillis() + hitForPassMillis)
        }
      f
```

The mapping function must not block: `ConcurrentHashMap.computeIfAbsent` holds a bin lock for its duration, so `fetch` returns a `Future` rather than a value.

## Purge and invalidation

CDNs offer three granularities of invalidation:

1. **Single-URL purge** — evicts exactly `https://ex.com/article/42`. Precise, but requires enumerating every affected URL.
2. **Wildcard or path purge** — evicts `https://ex.com/blog/*`. Coarse, and typically slower.
3. **Surrogate-key or cache-tag purge** — the origin tags each response with a `Surrogate-Key` header (Cloudflare's equivalent is `Cache-Tag`) listing the logical groups the object belongs to. Purging a key evicts every object carrying it.

A product page and a category page may both embed product 812:

```http
# response for /product/812
Surrogate-Key: product-812 category-shoes brand-acme

# response for /category/shoes
Surrogate-Key: category-shoes
```

**Keys are space-separated and the relation is many-to-many**: one object carries several keys, one key spans many objects. When product 812 changes, a single call purges every object referencing it:

```bash
curl -X POST https://api.fastly.com/service/$SERVICE/purge/product-812 \
  -H "Fastly-Key: $TOKEN"
```

`/product/812` and any other object tagged `product-812` are invalidated; the category page, tagged only `category-shoes`, remains cached. **This decouples TTL length from edit latency**: long TTLs sustain the hit ratio while tag purges propagate edits without waiting for expiry. Fastly also documents *soft* purge, which marks objects stale rather than evicting them, so `stale-while-revalidate` and `stale-if-error` behaviour still applies.

## Dynamic content

Three mechanisms cover content that is not a static blob:

- **Edge Side Includes (ESI).** The page shell is cached with a long TTL and a fragment is marked `<esi:include src="/cart-summary"/>`; the edge assembles the per-request portion. Supported in Varnish and Fastly VCL; Varnish enables parsing with `set beresp.do_esi = true;` on the backend response.
- **stale-while-revalidate at the edge.** The stale copy is returned immediately and revalidation proceeds in the background. `stale-if-error` extends the same tolerance across origin failures.
- **Compute at the edge.** Fastly Compute (WebAssembly) and Cloudflare Workers execute code inside the PoP — computing cache keys, performing authorization, synthesizing responses.

## Hit ratio arithmetic

Hit ratio is hits / (hits + misses); the operationally relevant quantity is its complement. **Origin load = (1 − hit ratio) × request rate.** At 10,000 requests per second, a 90% ratio yields 1,000 rps at origin and a 99% ratio yields 100 rps. **Moving from 98% to 99% halves origin traffic** — the same arithmetic that makes a purge-all severe, since it drives the ratio to 0% against an origin provisioned for 1% of request volume.

## Pitfalls

- **`Vary: User-Agent` on a cacheable page.** Symptom: near-zero hit ratio despite correct TTLs. Cause: the cache stores one entry per user-agent string, and user-agent strings differ per browser build.
- **`Age: 0` on every response.** Symptom: origin traffic matches client traffic. Cause: the object is not being stored — commonly `Cache-Control: private`, a `Set-Cookie` on the response, or a non-cacheable method — regardless of the status header.
- **Request collapsing on uncacheable responses.** Symptom: latency grows linearly with concurrency on a dynamic endpoint. Cause: waiters cannot share a `private` response and are served serially; a hit-for-pass object is required to release them.
- **`Surrogate-Control` expected to reach the client.** Symptom: the header is absent in browser developer tools. Cause: Fastly strips `Surrogate-Control` before delivering the response.
- **Surrogate keys that omit an embedding page.** Symptom: a product edit appears on `/product/812` but not on the category listing that embeds it. Cause: the listing response was never tagged `product-812`, so the key purge does not reach it.
- **Purge-all as a routine deployment step.** Symptom: origin saturates immediately after release. Cause: the hit ratio drops to zero at once, and origin capacity is sized for the miss fraction, not the full request rate.
