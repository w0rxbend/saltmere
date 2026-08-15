---
title: "Retries: exponential backoff, jitter, and retry budgets"
date: 2026-08-10
track: microservices
summary: "A naive retry loop is a load amplifier: when a dependency slows down, every client piles on and holds it there. Exponential backoff spreads the attempts, jitter de-synchronizes them, and a retry budget caps total retry volume so a transient fault cannot become a self-sustaining outage."
reading_time: 6
tags: [retries, backoff, jitter, retry-budget, resiliency, metastable-failure]
sources:
  - title: "Marc Brooker (AWS Architecture Blog) — Exponential Backoff And Jitter"
    url: "https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/"
  - title: "Marc Brooker (AWS Builders' Library) — Timeouts, retries, and backoff with jitter"
    url: "https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/"
  - title: "gRPC — Retry guide (retryThrottling / token bucket)"
    url: "https://grpc.io/docs/guides/retry/"
  - title: "Finagle — Retry Budgets"
    url: "https://finagle.github.io/blog/2016/02/08/retry-budgets/"
  - title: "Marc Brooker — Jitter: Making Things Better With Randomness"
    url: "https://brooker.co.za/blog/2015/03/21/backoff.html"
---

**Gist.** A retry is a bet that the failure was transient, and an unspaced retry adds its load to a dependency exactly when that dependency has the least headroom. Two mechanisms bound the bet: **capped exponential backoff with jitter**, which spaces and de-synchronizes one client's attempts, and a **retry budget**, a token bucket that caps how many retries the fleet may issue relative to primary traffic. Both cost latency and complexity — a jittered wait delays recovery for requests that would have succeeded on the second attempt, and a depleted budget refuses retries that individually would have worked.

The [timeouts-and-bulkheads article](/articles/microservices/2026-07-26-timeouts-retries-bulkheads) on this journal establishes the framing. This article goes deeper on the two knobs: attempt spacing and aggregate retry volume.

## Why unbounded retries amplify an outage

Consider a service running near capacity. A deploy, a garbage-collection (GC) pause, or a brief network fault pushes it over the edge and a slice of requests begin failing. If every client retries immediately, that retry traffic is *added on top of* the primary request rate, so offered load rises at the moment the service has the least spare capacity. More requests fail, which produces more retries, which raises load further.

This is a **retry storm**, and it is a route into a **metastable failure**: a state in which the system does not recover on its own after the original trigger is gone, because the retry traffic has *become* the sustaining load. Removing the deploy, repairing the network or ending the GC pause does not clear it; the remedies are added capacity or shedding the retries. Brooker's AWS Builders' Library article frames retries as "selfish" — a retrying client optimizes for itself at the expense of the shared backend — so the bound must be imposed by the system rather than by each client's local judgement.

## Capped exponential backoff, and its synchronization failure

The first mechanism is to stop retrying immediately. **Capped exponential backoff** doubles the wait after each attempt up to a ceiling:

```
sleep = min(cap, base * 2 ** attempt)
```

With `base = 100 ms` and `cap = 20 s`, the waits are 100 ms, 200 ms, 400 ms, 800 ms, and so on, saturating at the cap. This spreads a single client's attempts and gives the backend time to drain its queue.

Backoff alone has a distinct failure mode: **synchronization**. If a shared dependency fails and affects many clients at the same instant, every client computes the same `base * 2 ** attempt` and wakes at the same moment. Load then arrives as sharp synchronized spikes at 100 ms, 200 ms, 400 ms — each spike capable of re-tripping the overload being backed off from. **Doubling the interval does not help when every client doubles in lockstep.** As Brooker states in [his blog](https://brooker.co.za/blog/2015/03/21/backoff.html), the objective is to spread the calls out in time, not only to wait longer.

## Jitter

Jitter randomizes each client's wait so that the spikes flatten toward a smoother arrival rate. AWS's "Exponential Backoff And Jitter" analysis compared three variants against plain capped backoff. The formulas, as published:

**Full jitter** — a wait drawn uniformly between zero and the backoff ceiling:

```
sleep = random(0, min(cap, base * 2 ** attempt))
```

**Equal jitter** — half the interval fixed, half randomized:

```
temp  = min(cap, base * 2 ** attempt)
sleep = temp / 2 + random(0, temp / 2)
```

**Decorrelated jitter** — the window grows from the *previous* sleep rather than the attempt count:

```
sleep = min(cap, random(base, sleep * 3))   # seed sleep = base
```

The simulation measured two quantities: total client work (calls made to complete all requests) and total time to complete them. **Every jittered strategy reduced client work substantially relative to un-jittered capped backoff, without a corresponding penalty in completion time.** Full jitter and decorrelated jitter came out ahead in that comparison. Equal jitter's fixed half means clients still cluster at the *start* of each window. Decorrelated jitter requires carrying the previous `sleep` value forward; full jitter carries no state between attempts, which is why it is the cheaper default absent a measurement favouring another variant.

## Guardrails on aggregate volume

Backoff and jitter shape *when* one client retries. They do not bound *how many* retries the fleet issues in aggregate. Four further guardrails do.

**Retry only idempotent, retryable operations.** A retry duplicates a side effect when the first attempt succeeded but its response was lost. A `GET` is safe to repeat; a `POST /charge` can double-charge unless the write is made repeat-safe first with [idempotency keys](/articles/microservices/2026-07-30-idempotency-keys-safe-retries). Gating must also consider the *error class*: timeouts, connection failures, `503` and `429` are candidates; `400` and `403` are not, because repetition does not change the answer.

**An attempt cap and a total deadline.** The attempt cap bounds work per request; pairing it with an end-to-end [deadline](/articles/microservices/2026-08-10-timeouts-deadlines-propagation) prevents a chain of backoffs from exceeding the caller's latency budget and stops retries firing against a request the caller has already abandoned.

**A retry budget (token bucket).** This is the mechanism that bounds fleet-wide amplification: retries are capped to a fraction of primary traffic, and once error rates spike the bucket drains and further retries are *refused*, so the fleet degrades to primary traffic instead of multiplying it. Two implementations document their parameters:

- **Finagle**'s default budget allows 20% of requests to be retried on top of a floor of 10 retries per second, tracked in a token bucket whose credits expire after 10 seconds.
- **gRPC** `retryThrottling` maintains a token bucket per server: each failed call decrements the count by 1, each successful call adds back `tokenRatio` (for example `maxTokens: 10, tokenRatio: 0.1`), and retries are not permitted while the count is at or below `maxTokens / 2`.

**A circuit breaker.** A [circuit breaker](/articles/microservices/2026-07-24-circuit-breakers-resilience4j) trips after sustained failure and fails fast for a cooldown, so no attempt reaches a backend that is down. Budget and breaker are complementary: the budget bounds retry *volume* under partial failure, the breaker halts calls entirely under total failure.

**`Retry-After`.** When a server returns `429` or `503` with a `Retry-After` header it states when to return. That value is the backend's own admission-control signal and takes precedence over a locally computed backoff.

### Implementation sketch (Scala)

The load-bearing pieces are the full-jitter draw, the deadline check that must precede the sleep, and the budget consulted *before* each retry rather than after.

```scala
import scala.concurrent.duration.*
import scala.util.{Random, Try, Success, Failure}

final class RetryBudget(ratio: Double = 0.1, ttl: FiniteDuration = 10.seconds):
  private var tokens = 0.0
  private var last   = System.nanoTime()

  private def decay(): Unit =
    val now     = System.nanoTime()
    val elapsed = (now - last).toDouble / ttl.toNanos
    tokens = tokens * math.max(0.0, 1.0 - elapsed)  // credits expire after ttl
    last   = now

  def deposit(): Unit  = synchronized { decay(); tokens += ratio }   // ratio credits per call
  def take(): Boolean  = synchronized {
    decay()
    if tokens >= 1.0 then { tokens -= 1.0; true } else false          // depleted: refuse
  }

def callWithRetry[A](
    fn: () => A,
    retryable: Throwable => Boolean,
    budget: RetryBudget,
    maxAttempts: Int = 3,
    base: FiniteDuration = 100.millis,
    cap: FiniteDuration = 20.seconds,
    deadlineNanos: Long): A =
  var attempt = 0
  budget.deposit()                                                    // once per primary call
  while true do
    Try(fn()) match
      case Success(a) => return a
      case Failure(e) =>
        attempt += 1
        if !retryable(e) || attempt >= maxAttempts || !budget.take() then throw e
        val ceiling = math.min(cap.toMillis, base.toMillis * (1L << (attempt - 1)))
        val sleepMs = Random.nextLong(ceiling + 1)                    // full jitter
        if System.nanoTime() + sleepMs * 1000000L > deadlineNanos then throw e
        Thread.sleep(sleepMs)
  throw new IllegalStateException("unreachable")
```

## Pitfalls

- **Backoff without jitter re-collides.** Clients knocked out by one event compute identical waits and arrive together at each doubling, re-tripping the overload the backoff was meant to relieve.
- **Retrying at every layer multiplies.** Three attempts at each of three nested hops is up to 27 calls at the leaf; the amplification is the product of the per-layer caps, not their sum.
- **Retrying a non-idempotent write duplicates the effect.** A charge whose response was lost in transit has already been applied; the retry applies it a second time.
- **A retry issued after the caller's deadline is pure load.** The caller has abandoned the request, so the attempt can only consume backend capacity, never produce a used result.
- **Ignoring `Retry-After` converts load-shedding into a storm.** The `429`/`503` is the backend's admission control; retrying earlier than it asks re-adds the load it is shedding.
- **A budget consumed only on failure never refills.** The bucket's credits come from successful primary traffic; without a deposit on each primary call the ratio has no denominator and retries are refused permanently.
- **`maxAttempts` counted per hop, not per request, hides the true fan-out.** Metrics that report retries per client look bounded while the leaf service sees the product across hops.
