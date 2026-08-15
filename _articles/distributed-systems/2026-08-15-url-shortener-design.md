---
title: "Design a URL Shortener: Base62, ID Generation, and the 301 vs 302 Question"
date: 2026-08-15
track: distributed-systems
summary: "The canonical system-design warm-up, done properly: capacity math for 100M URLs/month, base62 encoding of a counter vs hashing the URL, why the redirect status code decides whether you get analytics, and a cache-in-front-of-KV layout that survives a hot link on a celebrity's feed."
reading_time: 5
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

"Design bit.ly" is the fizzbuzz of system design interviews, which is exactly why you should be able to do it crisply. The functional surface is tiny — shorten a URL, redirect on hit — so the interview is really about whether you can do back-of-envelope math, pick an encoding scheme with a reason, and scale a read path that is 100x hotter than the write path.

## API and capacity envelope

Two endpoints:

```
POST /api/urls        {"long_url": "...", "expiry": "..."}  -> {"short": "https://s.io/aK3x9Zb"}
GET  /{code}          -> 301/302 Location: <long_url>
```

Assume 100M new URLs/month and a 100:1 read:write ratio (ByteByteGo and AlgoMaster both land in this range; AlgoMaster's variant works through ~1M/day with 12k peak redirects/s):

- **Writes:** 100M / (30 × 86,400s) ≈ **40/s**, maybe 100/s at peak.
- **Reads:** 100 × that ≈ **4,000/s average, ~10k/s peak**.
- **Storage:** keep records 5 years → 6B rows. At ~500 bytes each (long URL dominates) → **~3 TB**. Tiny — this fits comfortably in one replicated KV store; the problem is QPS, not bytes.
- **Cache:** 80/20 rule → caching 20% of daily redirects ≈ 0.2 × 350M lookups × 500 B ≈ **~35 GB**. One large Redis box, or a small cluster for HA.

The takeaway to say out loud: this is a **read-heavy, latency-sensitive, tiny-data** system. Everything downstream follows from that.

## Short code: base62 over what, exactly?

Base62 (`0-9a-zA-Z`) is just a radix change — the interesting decision is *what number you encode*.

```python
ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

def base62(n: int) -> str:
    s = []
    while n:
        n, r = divmod(n, 62)
        s.append(ALPHABET[r])
    return "".join(reversed(s)) or "0"

# base62(2_100_000_000) -> '2i7nVK'  (6 chars)
```

Six characters give 62^6 ≈ 56.8B codes; seven give 62^7 ≈ 3.5T. For 6B URLs over five years, **7 characters is the safe answer**.

| Approach | How | Collisions | Predictable? | Notes |
|---|---|---|---|---|
| Hash long URL (MD5/SHA) + truncate | take first 7 base62 chars of digest | Yes — must check DB and re-salt on collision | No | Same URL dedupes for free |
| Encode auto-increment counter | `base62(next_id)` | Never | Yes — codes are enumerable | Needs a distributed counter |
| Encode a random/pre-generated key | offline Key Generation Service hands out unused codes | Never (KGS enforces) | No | Extra service to run |

Truncated hashing forces a read-check-retry loop on every write. A counter is collision-free but a single `AUTO_INCREMENT` is a SPOF and a bottleneck, so distribute it: either hand each app server a **range lease** (server A gets IDs 1–1M, B gets 1M–2M, from ZooKeeper or a `counters` table) or mint **Snowflake-style time-ordered IDs** — the corpus already covers that design in the [distributed unique IDs article](/articles/distributed-systems/2026-08-11-distributed-unique-ids-snowflake-uuidv7-ulid), so in an interview just name it and move on. If enumerability bothers you (competitors scraping `s.io/aK3x9Za`, `aK3x9Zb`, ...), bijectively scramble the ID before encoding or use the pre-generated-key service.

## 301 vs 302: the analytics trade-off

This is the detail that separates candidates who have thought about it:

- **301 Moved Permanently** — browsers and search engines treat it as permanent; MDN notes clients cache it and search engines transfer link equity to the target. After the first hit, the browser redirects *locally* and **your server never sees the click again**. Lowest load, zero analytics.
- **302 Found** — temporary; the client re-requests you every time. Every click hits your edge, so you can count clicks, referrers, geo, device — which is bit.ly's actual product.

Rule of thumb: **302 if analytics are the business, 301 if pure redirect throughput is** — and say it as a trade-off, not a fact. (If you must preserve the method on POST-ish flows, the modern pairs are 308/307.) Log the click as a non-blocking async event — push to Kafka, aggregate later — never a synchronous DB write on the redirect path.

## Storage and the read path

The data is a single key→value mapping with no joins and no cross-row transactions. A relational DB works at this scale, but the shape screams **KV/wide-column store** (DynamoDB, Cassandra) partitioned by short code — consistent hashing distributes codes uniformly since they're effectively random.

Read path: `GET /{code}` → CDN/edge (optional) → LB → app → **Redis** (`code → long_url`, LRU/TTL) → DB on miss. With the 20% cache holding the hot set, cache hit rates north of 90% are normal; DB sees hundreds of QPS, not 10k. Handle misses for *nonexistent* codes too: a *negative cache* or a Bloom filter of issued codes stops enumeration scans from hammering the DB.

**Hot links** are the classic follow-up: one code goes viral and a single Redis shard or DB partition takes the whole spike. Answers, in escalating order: (1) it's cached, so the shard serves it from memory anyway; (2) replicate hot keys to N cache nodes and pick one randomly per request (`aK3x9Zb#1..N`); (3) cache at the CDN edge with a short TTL, or return a 301 *with a bounded `Cache-Control: max-age`* so repeat clickers self-serve while analytics stay approximately right. That last hybrid — permanent-ish redirect, short cache lifetime — is what you'd actually ship.

Writes are boring by comparison: rate-limit per API key (token bucket), validate/canonicalize the URL, check a denylist for malware domains, insert, done.

**Try next:** build the whole thing in ~150 lines — Flask + SQLite + the base62 counter above — then add a Redis cache and use `wrk` to measure p99 latency at 1k RPS with 301 vs 302 responses; the difference in requests actually reaching your server is the analytics trade-off made visible.
