---
title: "Rate Limiting and Load Shedding: the Token Bucket, and Knowing When to Say 503"
date: 2026-07-31
track: microservices
summary: "A token bucket caps a caller's rate and still tolerates bursts; load shedding drops work when the server itself is overloaded. They answer different questions — 429 reports that a caller exceeded its quota, 503 reports that the server is saturated — and adaptive concurrency limits derive the ceiling from observed latency."
reading_time: 7
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

**Gist.** Two distinct failures present the same symptom — rejected requests — but have different causes: a single caller exceeding its allotted share, and a server exceeding its own capacity. A token bucket bounds the first by metering admissions against a continuously refilling credit balance; load shedding bounds the second by rejecting work cheaply before it reaches expensive downstream calls. Both trade completed work for preserved latency: requests that would have been served under a more permissive policy are refused so that the admitted set keeps its response-time distribution.

## The token bucket

A **token bucket** holds up to `b` tokens, starts full, and refills at `r` tokens per second. Each admitted request removes one token; an empty bucket forces a decision between rejection and waiting. Two invariants follow directly from the accounting:

- **The sustained rate over a long window converges to `r`,** because tokens can only be spent as fast as they accrue once the initial fill is exhausted.
- **The maximum burst is `b`,** because the bucket cannot hold more credit than its capacity. `b` is the parameter that decides how much short-term unevenness the limiter tolerates.

Implementations do not add tokens in discrete lumps on a timer. They accrue continuously, computing the balance lazily at the moment of the next request:

```
tokens = min(b, tokens_last + (now - t_last) * r)
```

The consequences are exact: **the time to earn one token is `1/r`**, and **the wait when `n` tokens are requested and only `available` are held is `(n - available) / r`**. Go's `x/time/rate` computes this formulation. The `min` with `b` is the load-bearing clamp — without it, an idle bucket would accumulate unbounded credit and a caller returning after an hour of silence could discharge an hour's worth of quota in one instant.

The **leaky bucket** is the complementary shape: it drains a queue at a fixed rate and emits a perfectly smooth output stream with **no burst tolerance**. It suits a fragile downstream that cannot absorb clustering; it penalises latency-sensitive callers, whose legitimate spikes are spread out rather than admitted.

A token-bucket limiter as HTTP middleware — 10 requests per second sustained, burst 30, overflow shed with a 429:

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

`Allow()` is the shed-on-empty path. Substituting `limiter.Wait(ctx)` converts the limiter from rejection to **throttling**: the caller blocks until a token is available. **The reject-versus-throttle decision is expressed entirely in the choice of method**, and it changes where the queue lives — in the client's blocked goroutine rather than in the client's retry logic.

The same algorithm appears in configuration form elsewhere. Envoy's local rate limit filter exposes `max_tokens`, `tokens_per_fill` and `fill_interval`. Resilience4j's `RateLimiter` uses a different discretisation: time is divided into `limitRefreshPeriod` cycles, and each cycle resets the permit count to `limitForPeriod`.

### Implementation sketch (Scala)

Continuous accrual with a compare-and-set loop, so concurrent callers cannot double-spend the same token:

```scala
import java.util.concurrent.atomic.AtomicReference

final case class Bucket(tokens: Double, lastNanos: Long)

final class TokenBucket(ratePerSec: Double, capacity: Double, nanoTime: () => Long):
  private val state = AtomicReference(Bucket(capacity, nanoTime()))

  /** Returns the wait in nanoseconds; 0 means admitted now. */
  def reserve(): Long =
    var out = 0L
    var done = false
    while !done do
      val cur = state.get()
      val now = nanoTime()
      val accrued = (now - cur.lastNanos) / 1e9 * ratePerSec
      val available = math.min(capacity, cur.tokens + accrued)
      // Deficit is allowed to go negative: the caller pays for it in waiting time.
      val next = Bucket(available - 1.0, now)
      if state.compareAndSet(cur, next) then
        out = if available >= 1.0 then 0L
              else ((1.0 - available) / ratePerSec * 1e9).toLong
        done = true
    out

  def allow(): Boolean = reserve() == 0L
```

`allow` discards the reservation's wait, which means a rejected call has still decremented the balance. A limiter that must not charge for shed requests has to restore the token on the reject path.

## 429 versus 503

The status code carries an attribution, not a cosmetic difference.

**429 Too Many Requests** states that *this caller* exceeded its quota. The decision is policy-driven, evaluated per key, typically at the edge, and `Retry-After` is meaningful because the limiter can compute when the next token accrues from the current balance — at most `1/r` seconds away.

**503 Service Unavailable** states that *the server* is saturated, independent of who is asking. This is **load shedding**: rejecting work **early and cheaply, before the expensive database call**, so that admitted requests retain their latency. The distinction that matters operationally is the input each decision reads. **Rate limiting reads a static quota and a per-caller counter; shedding reads a dynamic health signal.** A degraded response — cached or reduced-fidelity output — is the intermediate option between serving in full and rejecting.

Both differ from **backpressure**, which propagates "slow down" *upstream* through bounded queues rather than dropping requests. The three compose by position: backpressure inside a service where an upstream exists to slow down, shedding at the ingress boundary where none does, per-tenant rate limiting layered on top.

## Deriving the ceiling instead of configuring it

A hardcoded rate limit is a constant chosen against a capacity that changes with deploys, instance types and co-tenancy. **Adaptive concurrency limits**, as implemented in Netflix's `concurrency-limits`, bound **in-flight requests** rather than arrival rate, and infer the bound from measured latency.

The grounding is **Little's Law**, `L = λ · W`: the useful concurrency equals throughput multiplied by latency. Its use here is diagnostic. **Latency above the observed minimum round-trip time (RTT) indicates queueing**, because the no-load RTT is the service time with no waiting component; the excess is time spent in a queue. The controller therefore behaves like a congestion window.

- The **Vegas** variant estimates `queue = limit · (1 − minRTT/sampleRTT)`, raising the limit when the estimated queue is small and lowering it as the queue grows.
- The **Gradient** variants scale the limit by the ratio of a baseline RTT to a recent sampled RTT, shrinking the limit as that ratio degrades; `Gradient2Limit` takes the baseline from a long-window exponential average of sampled RTTs rather than from an all-time minimum.
- A separate **AIMD** (additive-increase/multiplicative-decrease) limiter is also provided: it increases the limit by a constant on success and multiplies it down on timeouts, without an RTT model.

The RTT-driven variants require no tuned constant for capacity, and tighten automatically when a real incident inflates latency.

## Pitfalls

- **A per-process limiter is not a fleet limiter.** Configuring `r` for the intended global rate while running `n` replicas admits `n · r`; every horizontal scale-up silently raises the effective quota.
- **A single global bucket lets one caller consume the entire budget.** Without a per-key bucket, a runaway retry loop drains the shared balance and every other tenant receives 429s caused by traffic they did not send.
- **An unclamped balance permits an arbitrarily large burst after idleness.** Omitting the `min(b, …)` clamp allows accrual without limit, so a long-silent caller discharges the whole accumulated quota at once.
- **Shedding after the expensive work has already been done saves nothing.** A 503 emitted after the database call has completed consumes the same capacity as a success and only removes the response from the caller.
- **429 without `Retry-After` invites immediate retry.** A client with no scheduled retry time typically retries at once, converting the rejection into additional load rather than removing load.
- **Rate limiting the arrival rate does not bound concurrency.** If latency rises, a fixed admission rate accumulates in-flight requests without limit; only a concurrency bound caps them.
- **A minimum RTT sampled during an ongoing overload is not the no-load RTT.** An inflated `minRTT` makes the Vegas queue estimate read near zero under real queueing, and the controller raises the limit while the service is already saturated.
