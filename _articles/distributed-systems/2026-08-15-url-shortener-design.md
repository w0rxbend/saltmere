---
title: "Designing a URL Shortener: Base62, ID Generation, and the 301 vs 302 Question"
date: 2026-08-15
track: distributed-systems
summary: "The canonical system-design warm-up: capacity math for 100M URLs per month, base62 encoding of a counter versus hashing the URL, how the redirect status code decides whether clicks remain observable, and a cache-in-front-of-key-value layout that survives a single viral link."
reading_time: 6
tags: [system-design, url-shortener, base62, caching, interview-prep]
sources:
  - title: "AlgoMaster — Design a URL Shortener"
    url: "https://blog.algomaster.io/p/design-a-url-shortener"
  - title: "ByteByteGo (Alex Xu) — Design A URL Shortener"
    url: "https://bytebytego.com/courses/system-design-interview/design-a-url-shortener"
  - title: "MDN — 301 Moved Permanently"
    url: "https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Status/301"
  - title: "MDN — 302 Found"
    url: "https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Status/302"
  - title: "Codesmith — Diagramming System Design: URL Shorteners"
    url: "https://codesmith.io/blog/diagramming-system-design-url-shorteners"
---

**Gist.** A shortening service maps an arbitrarily long uniform resource locator (URL) to a compact code and redirects on lookup; the functional surface is two endpoints, so the design problem is entirely one of read amplification — reads outnumber writes by roughly two orders of magnitude. The mechanism is a collision-free short code (base62 encoding of a distributed counter) placed behind an in-memory cache in front of a key-value store. The cost is that the choice of redirect status code trades server load against click observability: a permanent redirect is cached by the client and removes subsequent hits from the server's view entirely.

## Interface and capacity envelope

Two endpoints suffice:

```
POST /api/urls        {"long_url": "...", "expiry": "..."}  -> {"short": "https://s.io/aK3x9Zb"}
GET  /{code}          -> 301/302 Location: <long_url>
```

Assume 100M new URLs per month and a 100:1 read-to-write ratio; the cited walkthroughs work in this range. The exact inputs matter less than the classification they produce.

- **Writes:** 100M / (30 × 86,400 s) ≈ **40/s**, with peaks some small multiple higher.
- **Reads:** 100× that, ≈ **4,000/s** average, and correspondingly higher at peak.
- **Storage:** retaining records for five years yields 6B rows; at ~500 bytes each — the long URL dominates the row — that is **~3 TB**. This fits a single replicated key-value store. The binding constraint is queries per second, not bytes.
- **Cache:** under an 80/20 access assumption, caching 20% of daily redirects requires ≈ 0.2 × 350M lookups × 500 B ≈ **35 GB**, one large Redis instance or a small cluster for availability.

The classification that follows from these numbers is **read-heavy, latency-sensitive, small-data**. Every downstream decision derives from it.

## Short code: base62 applied to which number

Base62 (`0-9a-zA-Z`) is a radix change; the load-bearing decision is *what integer is encoded*. Six characters address 62^6 ≈ 56.8B codes, seven address 62^7 ≈ 3.5T. For a 6B-row corpus, **seven characters leaves headroom without a collision-avoidance loop**.

| Approach | Method | Collisions | Enumerable | Notes |
|---|---|---|---|---|
| Hash the long URL and truncate | base62 of a truncated MD5/SHA digest, 7 characters kept | Yes — requires a database check and re-salt | No | Identical URLs deduplicate without extra work |
| Encode an auto-increment counter | `base62(nextId)` | Never | Yes | Requires a distributed counter |
| Encode a pre-generated key | an offline key generation service (KGS) issues unused codes | Never, by construction | No | An additional service to operate |

Truncated hashing forces a read-check-retry loop on **every** write, because a truncated digest carries no uniqueness guarantee. A counter is collision-free, but a single `AUTO_INCREMENT` column is both a single point of failure and a write bottleneck. Two ways to distribute it: give each application server a **range lease** (server A holds IDs 1–1,000,000, server B 1,000,001–2,000,000, allocated from ZooKeeper or a `counters` table), or mint **Snowflake-style time-ordered identifiers**, covered separately in the [distributed unique IDs article](/articles/distributed-systems/2026-08-11-distributed-unique-ids-snowflake-uuidv7-ulid). Counter-derived codes are enumerable: an observer holding `s.io/aK3x9Za` can guess neighbouring codes. Applying a bijective scramble to the identifier before encoding, or switching to the pre-generated key service, removes that property.

### Implementation sketch (Scala)

The load-bearing pieces are the radix conversion and the range lease that keeps the counter off the critical path.

```scala
object Base62:
  private val Alphabet: IndexedSeq[Char] =
    ('0' to '9') ++ ('a' to 'z') ++ ('A' to 'Z')

  def encode(n: Long): String =
    require(n >= 0)
    if n == 0 then "0"
    else
      val out = StringBuilder()
      var rest = n
      while rest > 0 do
        out.append(Alphabet((rest % 62).toInt))
        rest /= 62
      out.result().reverse   // digits were emitted least-significant first

/** Hands out identifiers from a leased range; only range exhaustion
  * touches the shared allocator, so writes cost one local increment. */
final class LeasedIds(blockSize: Long, claimBlock: Long => Long):
  private var next: Long = 0L
  private var limit: Long = 0L

  def nextCode(): String = synchronized:
    if next >= limit then
      next = claimBlock(blockSize)   // durable compare-and-set upstream
      limit = next + blockSize
    val id = next
    next += 1
    Base62.encode(id)
```

`claimBlock` is the only durable operation, and it runs once per `blockSize` writes. A server crash abandons the remainder of its lease; identifiers are lost, uniqueness is not.

## 301 versus 302

- **301 Moved Permanently.** MDN records that the response is cacheable by default and that search engines update their links to the new URL. After the first hit the browser performs the redirect locally, so **the server observes no subsequent request for that code from that client**. Load is minimal and click counting degrades to counting first visits per client.
- **302 Found.** The redirect is temporary and the client re-requests the shortener on every click, making click counts, referrers, and geography observable at the cost of serving every hit.

The choice is a trade-off rather than a fact: **302 when click data is the product, 301 when redirect throughput is**. Where request method preservation matters, the corresponding codes are 308 and 307. Click recording belongs off the redirect path — an asynchronous event pushed to a log such as Kafka, aggregated later — because a synchronous database write adds its latency to every redirect.

## Storage and the read path

The data is a single key-to-value mapping with no joins and no cross-row transactions. A relational database is adequate at this volume, but the access shape fits a **key-value or wide-column store** (DynamoDB, Cassandra) partitioned by short code; codes are effectively random, so consistent hashing spreads them uniformly.

The read path is `GET /{code}` → optional content delivery network (CDN) edge → load balancer → application → **Redis** (`code → long_url`, least-recently-used eviction with a time-to-live) → database on miss. With the hot set resident, the database absorbs the miss traffic rather than the full peak. Misses for *nonexistent* codes need handling as well: a **negative cache** entry or a Bloom filter over issued codes prevents an enumeration scan from converting into one database read per probe.

**Hot keys** are the standard follow-up: a single code goes viral and one cache shard or database partition receives the entire spike. Escalating responses: the entry is cached, so the shard serves it from memory; replicate the hot key across N cache nodes under suffixed names (`aK3x9Zb#1..N`) and select one at random per request; or cache at the CDN edge with a short TTL, equivalently returning a redirect with a bounded `Cache-Control: max-age` so that repeat clickers self-serve while click counts remain approximately correct. That hybrid — a durable-looking redirect with a short cache lifetime — is the shippable compromise.

The write path is comparatively simple: rate-limit per API key with a token bucket, canonicalise and validate the URL, check a denylist for malware domains, insert.

## Pitfalls

- **Truncating a hash without a uniqueness check.** Two distinct URLs share a 7-character prefix and the second insert silently overwrites the first, sending its visitors to the wrong destination.
- **Serving 301 and then attempting to measure clicks.** Counts flatten after each client's first visit, because the browser satisfies later redirects from its own cache without contacting the server.
- **A single `AUTO_INCREMENT` counter.** Write throughput is capped by one row's lock, and the node holding it is a single point of failure for all shortening requests.
- **Sequential codes with no scramble.** Codes are enumerable, so a scraper walks the keyspace and reads every URL ever shortened.
- **No negative caching.** Requests for nonexistent codes miss the cache by definition and reach the database on every attempt, so an enumeration scan behaves as a database load generator.
- **Synchronous click logging.** The write latency of the analytics store is added to the redirect's p99, and an analytics outage becomes a redirect outage.
- **Assuming the lease block is recoverable after a crash.** Identifiers between the last issued value and the lease limit are permanently skipped; a design that requires gapless codes is incompatible with range leasing.
