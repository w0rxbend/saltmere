---
title: "Rate Limiting Windows: Fixed, Sliding Log, Sliding Counter — and Making Them Distributed"
date: 2026-08-15
track: microservices
summary: "Token bucket answers 'how do I allow bursts'; window algorithms answer 'how do I count requests per interval' — and each counts differently. Fixed windows let 2x the limit through at the boundary, sliding logs are exact but cost memory per request, and Cloudflare's sliding window counter approximates the log with two integers. Here is how each works, the Redis Lua script that makes counting atomic across instances, and what a correct 429 response looks like under the IETF RateLimit headers draft."
reading_time: 5
tags: [rate-limiting, sliding-window, redis, lua, http-429, distributed-systems]
sources:
  - title: "How we built rate limiting capable of scaling to millions of domains — Cloudflare Blog"
    url: "https://blog.cloudflare.com/counting-things-a-lot-of-different-things/"
  - title: "RateLimit header fields for HTTP — draft-ietf-httpapi-ratelimit-headers (IETF HTTPAPI WG)"
    url: "https://datatracker.ietf.org/doc/draft-ietf-httpapi-ratelimit-headers/"
  - title: "RFC 6585 — Additional HTTP Status Codes (429 Too Many Requests)"
    url: "https://www.rfc-editor.org/rfc/rfc6585"
  - title: "Redis rate limiter — Redis Docs"
    url: "https://redis.io/docs/latest/develop/use-cases/rate-limiter/"
  - title: "Scaling your API with rate limiters — Stripe Engineering"
    url: "https://stripe.com/blog/rate-limiters"
---

The [token bucket article](/articles/microservices/2026-07-31-rate-limiting-load-shedding-token-bucket/) covered shaping traffic and shedding load. This one covers the other family interviewers ask about: **window algorithms**, which enforce "N requests per interval" by counting. The whole design space is one trade: how precisely you count versus how much state you store — and then how you make that counting atomic when the limiter runs on twenty gateway instances.

## Fixed window: cheap, wrong at the edges

**Fixed window** buckets time into aligned intervals (`12:00:00–12:00:59`), keeps one counter per client per interval, and rejects when the counter passes the limit. One integer per key, trivially implemented as Redis `INCR` + `EXPIRE`.

The flaw is the **boundary burst problem**. With a limit of 100/minute, a client can send 100 requests at 12:00:59 and 100 more at 12:01:00 — 200 requests in two seconds, all accepted, because they landed in different buckets. Any interviewer who asks about fixed windows is fishing for this: the worst case admits **2x the limit** across a window boundary.

## Sliding window log: exact, expensive

**Sliding window log** stores a timestamp for *every* accepted request (in Redis, a sorted set keyed by client, scored by timestamp). On each request: drop entries older than `now - window`, count what remains, accept if under the limit. It is exact — no boundary artifact, ever — which is why Stripe's engineering post reaches for it when precision matters.

The cost is **memory proportional to the limit**: a 10,000 req/min limit means up to 10,000 timestamps *per client key*, plus a `ZREMRANGEBYSCORE` on every request. Fine for expensive endpoints with low limits (login attempts, password resets); ruinous at CDN scale.

## Sliding window counter: Cloudflare's approximation

**Sliding window counter** keeps fixed-window cheapness and kills most of the boundary problem. Store one counter for the current fixed window and one for the previous. Estimate the rolling count by weighting the previous window by how much of it still overlaps the sliding window:

```
estimate = prev_count * (1 - elapsed_fraction) + curr_count
```

At 15 seconds into a 60-second window, the previous window contributes 75% of its count. The assumption — traffic within the previous window was evenly spread — is an approximation, but Cloudflare's production analysis found it holds up remarkably well: across 400 million requests, only 0.003% were wrongly allowed or blocked, and the average rate error was ~6%. Two integers per key, no sorted sets, and the error always errs on the recent past rather than ignoring it. This is what Cloudflare runs across millions of domains.

| | Fixed window | Sliding log | Sliding counter |
|---|---|---|---|
| **State per key** | 1 counter | 1 timestamp per request | 2 counters |
| **Accuracy** | Up to 2x limit at boundary | Exact | ~Exact (bounded smoothing error) |
| **Cost per request** | O(1) | O(log n) + purge | O(1) |
| **Redis primitive** | `INCR` + `EXPIRE` | `ZADD`/`ZREMRANGEBYSCORE` | 2 `GET` + `INCR` |
| **Use when** | Rough quotas, billing periods | Low limits, high stakes | Default at scale |

## Making it distributed: Redis + Lua

With multiple gateway instances, `GET` then `INCR` as separate commands is a **check-then-act race**: two instances read 99, both conclude "under 100," both increment — the limit leaks under exactly the burst conditions it exists for. Redis executes a Lua script atomically, which closes the race. Here is a sliding window counter as one atomic script:

```lua
-- KEYS[1] = client key prefix
-- ARGV: [1] limit, [2] window_ms, [3] now_ms
local limit    = tonumber(ARGV[1])
local window   = tonumber(ARGV[2])
local now      = tonumber(ARGV[3])
local curr_win = math.floor(now / window)
local curr_key = KEYS[1] .. ":" .. curr_win
local prev_key = KEYS[1] .. ":" .. (curr_win - 1)

local prev = tonumber(redis.call("GET", prev_key) or "0")
local curr = tonumber(redis.call("GET", curr_key) or "0")
local elapsed = (now % window) / window
local estimate = prev * (1 - elapsed) + curr

if estimate >= limit then
  return {0, math.floor(estimate)}          -- rejected
end
redis.call("INCR", curr_key)
redis.call("PEXPIRE", curr_key, window * 2) -- previous window must survive one more
return {1, math.floor(estimate) + 1}        -- allowed
```

Load it once with `SCRIPT LOAD` and invoke by SHA with `EVALSHA`. Note the `window * 2` expiry — the current window's counter becomes next window's "previous," so it must outlive its own interval.

One Redis round trip per request is still a round trip. The next optimization — the one Cloudflare's post describes — is **local counting with async sync**: each instance counts in memory and periodically merges into the shared store, reading back the global total. You trade a small enforcement lag (a client can briefly exceed the limit by roughly `instances × sync_interval × rate`) for taking Redis off the hot path. For abuse protection that lag is fine; for hard billing quotas, stay atomic.

## What the 429 should look like

Rejections are an API contract. **RFC 6585** defines `429 Too Many Requests` and says the response *may* include **`Retry-After`** — send it; well-behaved SDKs sleep on it instead of hammering you. The IETF HTTPAPI working group's **ratelimit-headers draft** (still an Internet-Draft as of draft-11, May 2026 — not yet an RFC, but already adopted by gateways like Envoy and Kong in place of the ad-hoc `X-RateLimit-*` trio) standardizes advertising quota state as structured fields:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 23
RateLimit-Policy: "per-minute";q=100;w=60
RateLimit: "per-minute";r=0;t=23
```

`RateLimit-Policy` declares the quota (`q`) and window (`w`); `RateLimit` reports remaining quota (`r`) and seconds until reset (`t`). Return these on *successful* responses too, so clients can pace themselves before the 429 — self-limiting clients are cheaper than rejected ones.

**Try next:** run the Lua script against a local Redis with `redis-cli --eval`, fire 200 requests across a window boundary, and confirm the sliding counter admits ~100 where a fixed-window `INCR` admits 200.
