---
title: "Design a Rate Limiter: Five Algorithms and the Distributed Problem"
date: 2026-08-10
track: microservices
summary: "Fixed-window, sliding-window log, sliding-window counter, token bucket, and leaky bucket — what each enforces, where each leaks, and how one global limit is held across a fleet with an atomic Redis script rather than a read-modify-write race."
reading_time: 7
tags: [rate-limiting, system-design, redis, token-bucket, sliding-window, distributed-systems]
sources:
  - title: "Cloudflare — How we built rate limiting capable of scaling to millions of domains (sliding window)"
    url: "https://blog.cloudflare.com/counting-things-a-lot-of-different-things/"
  - title: "Stripe — Scaling your API with rate limiters"
    url: "https://stripe.com/blog/rate-limiters"
  - title: "IETF draft-ietf-httpapi-ratelimit-headers — RateLimit header fields for HTTP"
    url: "https://datatracker.ietf.org/doc/html/draft-ietf-httpapi-ratelimit-headers"
  - title: "Redis Docs — Rate limiter patterns (INCR / token bucket)"
    url: "https://redis.io/docs/latest/develop/use-cases/rate-limiter/"
  - title: "MDN — HTTP 429 Too Many Requests and Retry-After"
    url: "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/429"
---

**Gist.** A rate limiter must answer one boolean per request — admit or reject — under a limit that is defined over a time window and, in a fleet, shared by every instance. The five classic algorithms differ in how much state per key they retain and therefore how faithfully they reproduce a true rolling window; enforcing the decision globally requires that read, update and decision happen as one indivisible operation. The cost is a network round-trip per request to a shared store, or, if that round-trip is refused, a limit that is only approximately global.

The operational counterpart — when to shed load and return 503 rather than 429 — is treated in [rate limiting and load shedding](/articles/microservices/2026-07-31-rate-limiting-load-shedding-token-bucket/).

## What the limiter decides

For each request keyed by an identity (application programming interface key, user identifier, internet protocol address), the limiter returns *allow* or *reject*. The algorithm, the storage and the response headers are machinery around that boolean and around the trade-off it encodes: **accuracy against cost**. An exact limiter never admits the N+1th request within any window position. A cheaper limiter admits it under some arrival patterns and returns memory or a network hop in exchange.

## The five classic algorithms

### 1. Fixed-window counter

Time is bucketed into fixed intervals — for instance each calendar minute — with one integer per key per window. Each request increments the counter; the request is rejected once the count exceeds the limit; the counter is discarded when the clock enters the next window. The whole mechanism is one `INCR` and one `EXPIRE`.

The defect is the **window boundary**. Under a limit of 100 per minute, a client may send 100 requests in the final second of 12:00 and 100 more in the first second of 12:01 — **200 requests inside a two-second span, twice the intended rate, without violating the rule as stated**. The counter carries no memory across the reset, so the interval over which the limit binds is the calendar window, not any rolling window.

### 2. Sliding-window log

One timestamp per request is stored in a sorted set. A decision drops every entry older than `now - window` and counts the remainder. This is **exact**: the limit is enforced over a true rolling window and no boundary burst exists. The cost is one stored entry per admitted request plus a range-trim on every call, so **state grows with traffic rather than with the limit** — memory and central processing unit cost peak on precisely the hot key that is under attack.

### 3. Sliding-window counter

The compromise Cloudflare [described building](https://blog.cloudflare.com/counting-things-a-lot-of-different-things/) to scale across millions of domains. Two integers per key are retained — the count in the current window and the count in the previous one — and the rolling rate is estimated by weighting the previous window by the fraction of it that still overlaps the sliding window:

```
weight = (window - elapsed_in_current) / window
rate   = previous_count * weight + current_count
```

Worked example at 50 requests per minute, 15 seconds into the current minute, with 42 requests in the previous minute and 18 so far in the current one:

```
rate = 42 * ((60 - 15) / 60) + 18
     = 42 * 0.75 + 18
     = 49.5   →  one further request trips the limit
```

State is two counters per key, independent of request volume, and the boundary burst is smoothed rather than ignored. **The estimate assumes requests were uniformly distributed within the previous window**; where they were not, the weighted term misstates the true rolling count. Cloudflare measured **0.003% of requests wrongly allowed or limited** against an exact log.

### 4. Token bucket

A bucket holds at most `B` tokens (the burst capacity), starts full, and refills at `R` tokens per second. Each request removes one token; an empty bucket yields a reject or a wait. Over a long span the admitted rate converges to `R`, while **a momentary burst of up to `B` requests is admitted at once**. [Stripe describes this shape](https://stripe.com/blog/rate-limiters): each user has a bucket, and each request removes a token from it. Tokens are accrued continuously from the elapsed time rather than added in lumps by a timer:

```
tokens = min(B, tokens + (now - last_refill) * R)
```

The `min` is load-bearing: without the cap, an idle key accumulates unbounded credit and its next burst is unbounded too.

### 5. Leaky bucket

A queue drained at a fixed rate. Requests enter the queue; the server removes them at a constant `R` irrespective of arrival bursts; a full queue overflows and the overflowing requests are rejected. Where the token bucket *permits* bursts, the leaky bucket *absorbs* them and emits an even output stream — appropriate when the downstream cannot tolerate spikes, and worse for latency-sensitive callers, who now wait in the queue. Steady-state rate matches the token bucket; burst behaviour is its mirror image.

## Decision table

| Algorithm | State per key | Boundary burst | Accuracy | Applicable when |
|---|---|---|---|---|
| Fixed window | 1 counter | Up to 2x at edge | Coarse | Cheapest; approximation acceptable |
| Sliding-window log | N timestamps | None | Exact | Correctness matters, low volume |
| Sliding-window counter | 2 counters | Smoothed | 0.003% misclassified (Cloudflare) | General-purpose default |
| Token bucket | tokens + timestamp | Burst up to B | Exact rate plus burst | Bursts are to be admitted |
| Leaky bucket | queue + timestamp | None (smoothed out) | Constant output | Downstream cannot absorb spikes |

## The distributed problem

A single process with a local counter is straightforward. The substantive question is how fifty stateless instances enforce a *single global* limit of 1000 requests per minute for one customer. Two answers, and they restate the accuracy-against-latency trade-off at fleet scale.

**Central exact.** Every instance reads and writes one shared store, typically Redis. The decision is accurate per request, but each request pays a network round-trip, and a read-modify-write split into separate commands races: **two instances read the same count, both evaluate the predicate as allowed, both write back the same incremented value, and one admission is lost from the count**. The repair is atomicity — the check and the decrement execute as one server-side operation.

**Local approximate.** Each instance limits against its own share (1000/50 = 20 each) and reconciles counts periodically. No per-request hop, and the path survives a Redis outage, but the limit is loose at the edges: an uneven load balancer, or a customer whose traffic lands mostly on one instance, produces early throttling on that instance and unused quota elsewhere.

### Atomic window counter with Redis

`INCR` is itself atomic, so the increment never races; the second command exists to garbage-collect the key. A `MULTI`/`EXEC` transaction runs the pair without another client's commands interleaving. Pipelining alone only saves round-trips — it batches the commands but does not make them one unit:

```
MULTI
INCR   ratelimit:{key}:{window}
EXPIRE ratelimit:{key}:{window} 60   # intended for the first INCR of the window
EXEC
```

### Atomic token bucket with a Lua script

Redis executes a Lua script atomically — no other command interleaves — so read, refill and decrement cannot race:

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

The script returns the decision and the residual token count in one hop; the `EXPIRE` reclaims buckets that fall idle. During a Redis outage the fallback is local approximate limiting, and whether that fallback fails open or fails closed is an explicit design decision.

### Implementation sketch (Scala)

The same invariant expressed in-process: refill is derived from elapsed time at read, never from a background timer, and the compare-and-set retry supplies the atomicity that the Lua script gets from single-threaded execution.

```scala
import java.util.concurrent.atomic.AtomicReference

final case class Bucket(tokens: Double, tsNanos: Long)

final class TokenBucket(capacity: Double, ratePerSec: Double, now: () => Long):
  private val state = AtomicReference(Bucket(capacity, now()))

  /** Returns true if `want` tokens were removed. Either way the refill is committed. */
  def tryAcquire(want: Double = 1.0): Boolean =
    var decided = false
    var result  = false
    while !decided do
      val cur     = state.get()
      val t       = now()
      val elapsed = math.max(0L, t - cur.tsNanos) / 1e9
      val refilled = math.min(capacity, cur.tokens + elapsed * ratePerSec)
      val next =
        if refilled >= want then Bucket(refilled - want, t)
        else Bucket(refilled, t)          // timestamp advances even on reject
      if state.compareAndSet(cur, next) then
        result  = refilled >= want
        decided = true
    result
```

`math.max(0L, ...)` matters where `now` is a wall clock: a backwards step would otherwise produce a negative elapsed term and remove tokens that were never spent.

## Signalling the outcome

A rejected request returns **HTTP 429 Too Many Requests** with a **`Retry-After`** header — a delay in seconds or an HTTP date — so conforming clients defer instead of retrying immediately. Servers may also advertise the live quota with the **`RateLimit`** and **`RateLimit-Policy`** fields from the IETF draft [RateLimit header fields for HTTP](https://datatracker.ietf.org/doc/html/draft-ietf-httpapi-ratelimit-headers), which cover the ground that ad-hoc `X-RateLimit-*` headers covered in individual application programming interfaces. The draft is a work in progress and its field names have changed between revisions, so a server that emits them cannot assume a client parses them. Emitting the fields on successful responses as well allows a client to self-throttle before it reaches a 429.

## Pitfalls

- **Fixed-window limits are breached by design at the boundary.** Traffic doubles across the reset instant because the counter retains nothing from the preceding window; a monitoring dashboard averaged per minute will not show it.
- **A sliding-window log grows with request volume, not with the limit.** The key that is being attacked is the key whose sorted set consumes the most memory and the most trim work per call.
- **The sliding-window counter's estimate degrades when the previous window was non-uniform.** A burst concentrated at the end of the previous minute is spread evenly by the weighting, so the estimated rate understates the true rolling count.
- **An uncapped token refill converts idleness into an unbounded burst.** Omitting the `min(B, ...)` cap lets a key that was quiet for an hour admit an hour's worth of tokens at once.
- **Split `GET` then `SET` against Redis loses admissions under concurrency.** Two instances read the same value, both admit, and both store the same incremented count, so the store records one request where two were served.
- **`EXPIRE` issued unconditionally on every request refreshes the window.** For the fixed-window key the expiry is meant to fire at the end of the interval; re-setting it on each increment extends the window for as long as traffic continues.
- **A wall clock that steps backwards corrupts elapsed-time refill.** Negative elapsed time subtracts tokens in the naive expression, so a bucket can be emptied by a clock adjustment rather than by traffic.
- **`Retry-After` omitted from a 429 invites immediate retry.** The client has no advertised delay and falls back to its own policy, which concentrates the retries that the limiter was rejecting.
