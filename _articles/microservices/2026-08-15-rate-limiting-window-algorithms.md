---
title: "Rate Limiting Windows: Fixed, Sliding Log, Sliding Counter — and Making Them Distributed"
date: 2026-08-15
track: microservices
summary: "Token bucket answers how bursts are absorbed; window algorithms answer how requests are counted per interval — and each counts differently. Fixed windows admit twice the limit across a boundary, sliding logs are exact but store one timestamp per request, and Cloudflare's sliding window counter approximates the log with two integers. This article covers each mechanism, the Redis Lua script that makes counting atomic across instances, and the shape of a conforming 429 response under the IETF RateLimit headers draft."
reading_time: 6
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

**Gist.** Enforcing "N requests per interval" requires counting requests against a time window, and the three standard counting schemes differ in how faithfully the window tracks real time. Fixed windows store one counter and admit up to twice the limit across a boundary; sliding logs store one timestamp per request and are exact; the sliding window counter stores two counters and estimates the rolling total by weighting the previous window. The cost paid is state per key and, once the limiter runs on more than one instance, an atomic round trip to shared storage on every request.

The [token bucket article](/articles/microservices/2026-07-31-rate-limiting-load-shedding-token-bucket/) covered shaping traffic and shedding load. This one covers **window algorithms**, whose entire design space is a single trade: counting precision against stored state, and then atomicity of that counting across instances.

## Fixed window: one counter, wrong at the edges

**Fixed window** partitions time into aligned intervals (`12:00:00–12:00:59`), keeps one counter per client per interval, and rejects once the counter passes the limit. State is one integer per key; in Redis the operation is `INCR` followed by `EXPIRE`.

The defect is the **boundary burst**. Under a limit of 100 per minute, a client sending 100 requests at 12:00:59 and 100 more at 12:01:00 has 200 requests accepted within two seconds, because the two batches were charged to different counters. The invariant the algorithm enforces is *at most `limit` per aligned interval*, not *at most `limit` in any interval of that length*; the worst case over an arbitrary window is therefore **2x the limit**.

## Sliding window log: exact, memory proportional to the limit

**Sliding window log** stores a timestamp for every accepted request — in Redis, a sorted set keyed by client and scored by timestamp. Each request drops entries older than `now - window`, counts the remainder, and accepts when the remainder is under the limit. The count is over a genuinely rolling interval, so **no boundary artifact exists at any offset**.

The cost is **memory proportional to the limit**: a limit of 10,000 requests per minute admits up to 10,000 timestamps per client key, plus a `ZREMRANGEBYSCORE` purge on every request. That is affordable for endpoints with low limits and high stakes — login attempts, password resets — and not affordable at content-delivery-network (CDN) request volumes.

## Sliding window counter: two integers per key

**Sliding window counter** retains fixed-window cost while removing most of the boundary error. It keeps one counter for the current fixed window and one for the previous, and estimates the rolling count by weighting the previous window by the fraction of it still overlapping the sliding interval:

```
estimate = prev_count * (1 - elapsed_fraction) + curr_count
```

At 15 seconds into a 60-second window, `elapsed_fraction` is 0.25 and the previous window contributes 75% of its count. The load-bearing assumption is that **traffic within the previous window was uniformly distributed**; where it was not, the estimate is wrong in proportion to the skew. Cloudflare's production analysis of this scheme reports that across 400 million requests, **0.003% were wrongly allowed or blocked, with an average rate error of roughly 6%**. State is two integers per key, with no sorted set and no per-request purge. This is the algorithm Cloudflare describes running across millions of domains.

| | Fixed window | Sliding log | Sliding counter |
|---|---|---|---|
| **State per key** | 1 counter | 1 timestamp per request | 2 counters |
| **Accuracy** | Up to 2x limit at boundary | Exact | Bounded smoothing error |
| **Cost per request** | O(1) | O(log n) + purge | O(1) |
| **Redis primitive** | `INCR` + `EXPIRE` | `ZADD`/`ZREMRANGEBYSCORE` | 2 `GET` + `INCR` |
| **Use when** | Rough quotas, billing periods | Low limits, high stakes | Default at scale |

## Making it distributed: Redis and Lua

Across multiple gateway instances, issuing `GET` and then `INCR` as separate commands is a **check-then-act race**: two instances read 99, each concludes the limit is not reached, and each increments. The limit leaks precisely under the concurrent burst it exists to contain. Redis executes a Lua script atomically with respect to other commands, which collapses read and write into one indivisible step:

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

The script is registered once with `SCRIPT LOAD` and invoked by digest with `EVALSHA`. The **`window * 2` expiry is load-bearing**: the current window's counter becomes the next window's `prev`, so it must outlive its own interval, and an expiry of exactly `window` would silently reset the previous-window term to zero.

One Redis round trip per request remains one round trip. The further optimisation Cloudflare's post describes is **local counting with asynchronous synchronisation**: each instance counts in memory and periodically merges into the shared store, reading back the global total. The exchange is enforcement lag — a client can exceed the limit by roughly `instances × sync_interval × rate` before the merge lands — for removing shared storage from the hot path. That lag is tolerable for abuse protection and not tolerable for hard billing quotas.

### Implementation sketch (Scala)

The estimator itself, isolated from storage. Two counters and a clock reading are the whole state.

```scala
final case class Window(index: Long, count: Long)

final class SlidingCounter(limit: Long, windowMs: Long):
  // Only the current and immediately preceding window are retained.
  private var curr = Window(-1L, 0L)
  private var prev = Window(-1L, 0L)

  private def roll(nowMs: Long): Unit =
    val idx = nowMs / windowMs
    if idx != curr.index then
      prev = if idx == curr.index + 1 then curr else Window(idx - 1, 0L)
      curr = Window(idx, 0L)

  /** Returns true when the request is admitted, and the estimate used. */
  def tryAcquire(nowMs: Long): (Boolean, Double) =
    synchronized:
      roll(nowMs)
      val elapsed  = (nowMs % windowMs).toDouble / windowMs
      val estimate = prev.count * (1.0 - elapsed) + curr.count
      if estimate >= limit then (false, estimate)
      else
        curr = curr.copy(count = curr.count + 1)
        (true, estimate + 1)
```

The `roll` branch matters: when more than one window has elapsed since the last request, the preceding window is genuinely empty and must be reset rather than carried forward, otherwise a stale count is weighted into the estimate.

## Rejection as an API contract

**RFC 6585** defines `429 Too Many Requests` and states the response may include **`Retry-After`**. Emitting it lets conforming clients sleep for the indicated interval rather than retrying immediately. The IETF HTTPAPI working group's **ratelimit-headers draft** — an Internet-Draft, not an RFC, intended to replace the ad-hoc `X-RateLimit-*` headers that gateways emit in mutually incompatible forms — specifies quota state as structured fields:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 23
RateLimit-Policy: "per-minute";q=100;w=60
RateLimit: "per-minute";r=0;t=23
```

`RateLimit-Policy` declares quota (`q`) and window (`w`); `RateLimit` reports remaining quota (`r`) and seconds until reset (`t`). Returning both on successful responses as well lets clients pace themselves before reaching a rejection.

A useful verification: run the Lua script against a local Redis with `redis-cli --eval`, issue 200 requests across a window boundary, and compare admissions against a fixed-window `INCR` limiter on the same trace.

## Pitfalls

- **Expiring the sliding-counter key after exactly one window.** The previous-window term reads as zero on the next interval, and the limiter degrades to a fixed window with its full 2x boundary admission.
- **Reading then incrementing as two Redis commands.** Concurrent gateway instances observe the same pre-increment count and all admit, so the limit is exceeded by up to the number of instances under simultaneous arrival.
- **Deriving `now_ms` from each gateway's local clock.** Clock skew between instances places requests in different window indices, splitting one client's count across keys. Passing the time from the caller, as the script above does, makes the skew the caller's rather than Redis's.
- **Applying the sliding-counter estimate to bursty traffic and expecting Cloudflare's error figures.** The reported 0.003% misclassification rate rests on traffic that approximates uniform distribution within a window; a client that sends its entire allowance in the last second of the previous window is over-weighted early and under-weighted late.
- **Sliding window log on a high limit.** Memory grows with the limit, not the request rate, so a 10,000-per-minute quota reserves capacity for 10,000 sorted-set members per active client key.
- **Omitting `Retry-After` from the 429.** Clients with no reset signal retry on their own schedule, and the rejected traffic continues to consume connection and routing capacity.
