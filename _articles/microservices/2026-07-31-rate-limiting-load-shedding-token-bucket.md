---
title: "Rate Limiting and Load Shedding: the Token Bucket, and Knowing When to Say 503"
date: 2026-07-31
track: microservices
summary: "A token bucket caps a caller's rate and still tolerates bursts; load shedding drops work when the server itself is drowning. They answer different questions — 429 means 'you went too fast', 503 means 'I'm overloaded' — and adaptive concurrency limits find the ceiling for you."
reading_time: 6
tags: [rate-limiting, load-shedding, token-bucket, resilience, backpressure, go]
sources:
  - title: "golang.org/x/time/rate — token bucket Limiter (Allow/Reserve/Wait)"
    url: "https://pkg.go.dev/golang.org/x/time/rate"
  - title: "Netflix/concurrency-limits — adaptive concurrency (Little's Law, Vegas/Gradient, AIMD)"
    url: "https://github.com/Netflix/concurrency-limits"
  - title: "Resilience4j RateLimiter docs"
    url: "https://resilience4j.readme.io/docs/ratelimiter"
  - title: "Envoy local rate limit filter (token bucket)"
    url: "https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/local_rate_limit_filter"
  - title: "Building Microservices, 2nd ed. — Sam Newman"
    url: "https://samnewman.io/books/building_microservices_2nd_edition/"
---

Two different failures wear the same T-shirt. One caller hammering your API with a runaway retry loop is a *quota* problem. Your whole fleet tipping past its capacity during a traffic spike is an *overload* problem. Rate limiting solves the first, load shedding solves the second, and conflating them is how you end up shedding your best customers while a broken script sails through.

## The token bucket

A **token bucket** holds up to `b` tokens (the capacity, and therefore the maximum burst), starts full, and refills at `r` tokens per second. Each admitted request removes one token; an empty bucket means reject or wait. Over any long window the sustained rate converges to `r`, but the bucket lets a momentary burst of up to `b` through — which is what you want, because real traffic is bursty and a hard per-second wall punishes legitimate spikes.

Good implementations don't add tokens in discrete lumps. They accrue continuously:

```
tokens = min(b, tokens_last + (now - t_last) * r)
```

so the time to earn one token is `1/r`, and the wait for `n` tokens when short is `(n - available) / r`. Go's `x/time/rate` computes exactly this. Its opposite number, the **leaky bucket**, drains a queue at a fixed rate and emits a perfectly smooth stream with *no* burst tolerance — better when you're protecting a fragile downstream, worse for latency-sensitive callers.

Here's a token-bucket limiter as HTTP middleware — 10 req/s sustained, burst 30, shedding the overflow with a 429:

```go
import "golang.org/x/time/rate"

// r = tokens/sec, b = burst/capacity
var limiter = rate.NewLimiter(rate.Limit(10), 30)

func rateLimit(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !limiter.Allow() {            // non-blocking: take a token or shed
			w.Header().Set("Retry-After", "1")
			http.Error(w, "rate limit exceeded", http.StatusTooManyRequests) // 429
			return
		}
		next.ServeHTTP(w, r)
	})
}
```

`Allow()` is the shed-on-empty path. Swap in `limiter.Wait(ctx)` and you *throttle* instead — block the caller until a token frees — which is the throttle-versus-reject choice in one method name. Envoy's local rate limit filter is the same algorithm in config form (`max_tokens`, `tokens_per_fill`, `fill_interval`), and Resilience4j's `RateLimiter` divides time into `limitRefreshPeriod` cycles that reset `limitForPeriod` permits.

## 429 versus 503

The status code is not cosmetic; it tells the caller whose fault it is.

**429 Too Many Requests** means *this caller* exceeded its quota. It's policy-driven and per-key, evaluated at the edge, and it's fair to pair with `Retry-After`. **503 Service Unavailable** means *the server* is overloaded right now, independent of who's asking. That's **load shedding**: a server protecting itself by rejecting work *early and cheaply* — before the expensive database call — so the requests it does accept keep their latency. Shedding is dynamic and health-driven; rate limiting is static and quota-driven. Under shed load, the graceful move is **degradation**: serve cached or reduced-fidelity results rather than failing hard.

Both are distinct from **backpressure**, which pushes "slow down" *upstream* through bounded queues instead of dropping. The clean composition: backpressure inside your service, load-shed at the ingress boundary where there's no upstream left to slow, rate-limit per-tenant on top.

## Let the server find its own ceiling

A hardcoded rate limit is a guess that rots — capacity changes with deploys, instance types, and neighbors. **Adaptive concurrency limits** (as in Netflix's `concurrency-limits`) bound *in-flight requests* instead and discover the limit from latency. The grounding is **Little's Law**, `L = λ · W`: useful concurrency ≈ throughput × latency. Overload shows up as latency climbing above the observed *minimum* (no-load) RTT — a sign requests are queueing — so the algorithm behaves like a TCP congestion window. The Vegas variant estimates `queue = limit · (1 − minRTT/sampleRTT)` and nudges the limit up when the queue is small, down when it grows; the Gradient variant tracks the ratio of a short- to long-window RTT and backs off multiplicatively (AIMD) when it detects the ratio dropping. No magic number to tune, and it tightens automatically under a real incident.

**Try next:** Put the Go middleware above in front of a handler that sleeps 50 ms, then hit it with a load tool at 5, 15, and 60 req/s and watch the 429 rate. Now add a second bucket keyed by an `X-Api-Key` header so one noisy client can't consume everyone's budget — and confirm a burst of 30 goes straight through before throttling kicks in.
