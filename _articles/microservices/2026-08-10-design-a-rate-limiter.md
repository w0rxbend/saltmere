---
title: "Design a Rate Limiter: Five Algorithms and the Distributed Problem"
date: 2026-08-10
track: microservices
summary: "The canonical system-design question, answered. Fixed-window, sliding-window log, sliding-window counter, token bucket, and leaky bucket — what each gets right, where each leaks, and how you enforce one global limit across a fleet with an atomic Redis script instead of a race."
reading_time: 7
tags: [rate-limiting, system-design, redis, token-bucket, sliding-window, distributed-systems]
sources:
  - title: "Cloudflare — How we built rate limiting capable of scaling to millions of domains (sliding window)"
    url: "https://blog.cloudflare.com/counting-things-a-lot-of-different-things/"
  - title: "Stripe — Scaling your API with rate limiters"
    url: "https://stripe.com/blog/rate-limiters"
  - title: "IETF draft-ietf-httpapi-ratelimit-headers-11 — RateLimit header fields for HTTP"
    url: "https://datatracker.ietf.org/doc/html/draft-ietf-httpapi-ratelimit-headers"
  - title: "Redis Docs — Rate limiter patterns (INCR / token bucket)"
    url: "https://redis.io/docs/latest/develop/use-cases/rate-limiter/"
  - title: "MDN — HTTP 429 Too Many Requests and Retry-After"
    url: "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/429"
---

"Design a rate limiter" is the interview question that looks like a five-minute answer and turns into forty. Anyone can say "token bucket." The signal the interviewer wants is whether you know *why* a fixed-window counter lets 2x traffic through at the window edge, when the memory cost of exact counting is worth it, and how you make one shared limit hold across fifty stateless instances without them racing each other. This article is the algorithm-comparison answer. If you want the operational side — when to shed load and return 503 instead of 429 — see the companion piece, [rate limiting and load shedding](/articles/microservices/2026-07-31-rate-limiting-load-shedding-token-bucket/).

## What a rate limiter actually decides

For each incoming request keyed by some identity (API key, user ID, IP), the limiter answers one boolean: *allow or reject*. Everything else — the algorithm, the storage, the headers — is machinery around that boolean and the trade-off it encodes: **accuracy versus cost**. An exact limiter never admits the N+1th request in any window; a cheap limiter admits it sometimes and saves you memory or a network hop in exchange. There is no free lunch, only a lunch you have chosen.

## The five classic algorithms

### 1. Fixed-window counter

Bucket time into fixed intervals — say each calendar minute — and keep one integer per key per window. Increment on each request; reject when the count exceeds the limit; the counter resets when the clock ticks to the next window. It is one `INCR` and one `EXPIRE`, which is why it is everywhere.

The flaw is the **window boundary**. With a limit of 100/min, a client can send 100 requests in the last second of 12:00 and another 100 in the first second of 12:01 — 200 requests in a two-second span, double the intended rate, entirely within the rules. The counter has no memory of what happened just before the reset.

### 2. Sliding-window log

Store a timestamp for every request in a sorted set. To decide, drop everything older than `now - window` and count what remains. This is **exact**: the limit is enforced over a true rolling window with no edge burst. The cost is that you store one entry per request and do a range-trim on every call, so a hot key under attack is exactly when memory and CPU balloon. Correct, but it scales with traffic instead of with the limit.

### 3. Sliding-window counter

The production compromise, and the one Cloudflare [described building](https://blog.cloudflare.com/counting-things-a-lot-of-different-things/) to scale across millions of domains. Keep just two integers — the count for the current window and the count for the previous one — and estimate the rolling rate by weighting the previous window by how much of it still overlaps the sliding window:

```
weight = (window - elapsed_in_current) / window
rate   = previous_count * weight + current_count
```

Worked example, 50 req/min, 15 seconds into the current minute, 42 requests last minute and 18 so far this minute:

```
rate = 42 * ((60 - 15) / 60) + 18
     = 42 * 0.75 + 18
     = 49.5   →  one more request trips the limit
```

Two numbers per key, no per-request storage, and it smooths the boundary burst instead of ignoring it. Cloudflare measured only **0.003% of requests wrongly allowed or limited** against an exact log — the approximation assumes a uniform request distribution in the previous window, which is close enough in practice. This is the common default.

### 4. Token bucket

A bucket holds up to `B` tokens (the burst capacity), starts full, and refills at `R` tokens/sec. Each request removes one token; an empty bucket means reject or wait. Over any long span the admitted rate converges to `R`, but the bucket lets a momentary burst of up to `B` through — which matches real traffic, which is bursty. This is the most popular choice and what [Stripe uses](https://stripe.com/blog/rate-limiters): "every Stripe user has a bucket, and every time they make a request we remove a token from that bucket." Good implementations accrue tokens continuously rather than in lumps:

```
tokens = min(B, tokens + (now - last_refill) * R)
```

### 5. Leaky bucket

A queue drained at a fixed rate. Requests enter the queue; the server processes them at a constant `R` regardless of arrival bursts; a full queue overflows and rejects. Where the token bucket *permits* bursts, the leaky bucket *absorbs and smooths* them into a perfectly even output stream — ideal when you are shielding a fragile downstream that cannot tolerate spikes, worse for latency-sensitive callers who now wait in line. It is the token bucket's mirror image: same steady-state rate, opposite burst behavior.

## Decision table

| Algorithm | State per key | Boundary burst | Accuracy | Best when |
|---|---|---|---|---|
| Fixed window | 1 counter | Up to 2x at edge | Coarse | Cheapest; approximate is fine |
| Sliding-window log | N timestamps | None | Exact | Correctness matters, low volume |
| Sliding-window counter | 2 counters | Smoothed | ~99.997% | The general-purpose default |
| Token bucket | tokens + timestamp | Burst up to B | Exact rate + burst | APIs that want to allow bursts |
| Leaky bucket | queue + timestamp | None (smoothed out) | Constant output | Protecting a fragile downstream |

## The distributed problem

One process with a local counter is trivial. The real question: how do fifty stateless instances enforce a *single global* limit of 1000 req/min for one customer? Two answers, and they are the accuracy-vs-latency trade-off in the large.

**Central exact.** Every instance reads and writes one shared store — Redis. Accurate to the request, but every request pays a network round-trip, and a naive read-modify-write races: two instances read the same count, both decide "allowed," both write back, and the limit is breached. The fix is **atomicity** — do the whole check-and-decrement in one server-side operation.

**Local approximate.** Each instance rate-limits against its own share (e.g. 1000/50 = 20 each), syncing counts periodically. No per-request hop, so it is fast and it survives a Redis outage, but it is loose at the edges — an uneven load balancer, or a customer whose traffic all lands on one instance, gets throttled early or admitted late.

### Atomic sliding window with Redis

The fixed/sliding-window counter is two commands, and pipelining or `MULTI` keeps them together. The `EXPIRE` is what garbage-collects idle keys:

```
INCR   ratelimit:{key}:{window}
EXPIRE ratelimit:{key}:{window} 60   # only meaningful on first INCR
```

### Atomic token bucket with a Lua script

Redis runs a Lua script atomically — no other command interleaves — so the read, refill, and decrement cannot race. This is the standard distributed token bucket:

```lua
-- KEYS[1] = bucket key
-- ARGV[1] = rate (tokens/sec)  ARGV[2] = capacity
-- ARGV[3] = now (sec)          ARGV[4] = requested tokens
local rate     = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local now      = tonumber(ARGV[3])
local want     = tonumber(ARGV[4])

local b       = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens  = tonumber(b[1]) or capacity
local ts      = tonumber(b[2]) or now

-- refill for elapsed time, capped at capacity
tokens = math.min(capacity, tokens + (now - ts) * rate)

local allowed = tokens >= want
if allowed then tokens = tokens - want end

redis.call('HMSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], math.ceil(capacity / rate))
return { allowed and 1 or 0, tokens }
```

The script returns the decision and the remaining tokens in one hop, the `EXPIRE` reclaims buckets that go idle, and because Redis is single-threaded per script there is no lock and no race. Under a Redis outage you fall back to local approximate limiting — fail open or fail closed is your call, and it is a real decision, not a footnote.

## Tell the client what happened

A rejected request should return **HTTP 429 Too Many Requests** with a **`Retry-After`** header (seconds, or an HTTP date) so well-behaved clients back off instead of hammering. Increasingly, servers also advertise the live quota with the **`RateLimit`** and **`RateLimit-Policy`** fields from [draft-ietf-httpapi-ratelimit-headers-11](https://datatracker.ietf.org/doc/html/draft-ietf-httpapi-ratelimit-headers), which standardize the ad-hoc `X-RateLimit-*` headers every API invented independently. When both `Retry-After` and the RateLimit fields are present, the draft says `Retry-After` takes precedence. Send these on *successful* responses too, so clients can self-throttle before they ever hit a 429.

**Try next:** Stand up the Lua token bucket on a local Redis, point a load generator at it from three processes at once, and confirm the global rate holds; then swap in the naive `GET`/`SET` version and watch the same test breach the limit — the gap you just measured is exactly why atomicity is the whole game.
