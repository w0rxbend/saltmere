---
title: "Exponential Backoff, Jitter, and the Anatomy of a Retry Storm"
date: 2026-08-15
track: microservices
summary: "Exponential backoff without jitter keeps failed clients in lockstep; Marc Brooker's AWS analysis measures the cost and separates the jitter variants. Then the harder arithmetic: retries multiplying through layers, retry budgets as the fleet-wide cap, and why a retry storm can hold a system down after the original trigger is gone."
reading_time: 7
tags: [retries, backoff, jitter, retry-storm, metastability, resilience]
sources:
  - title: "Marc Brooker — Exponential Backoff and Jitter (AWS Architecture Blog)"
    url: "https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/"
  - title: "Bronson et al. — Metastable Failures in Distributed Systems (HotOS '21)"
    url: "https://sigops.org/s/conferences/hotos/2021/papers/hotos21-s11-bronson.pdf"
  - title: "Finagle User's Guide — Clients: Retries and RetryBudget"
    url: "https://twitter.github.io/finagle/guide/Clients.html"
  - title: "Linkerd — Retries and Timeouts (retry budgets)"
    url: "https://linkerd.io/2.15/features/retries-and-timeouts/"
  - title: "Google SRE Book, Ch. 22 — Addressing Cascading Failures"
    url: "https://sre.google/sre-book/addressing-cascading-failures/"
---

**Gist.** Failures in a distributed system are correlated, so a retry policy that is polite for one client can be a synchronized load multiplier for a fleet: every client observes the same error at the same instant and returns at the same instant. Randomising the wait (jitter) breaks the synchronisation, and bounding retries as a *ratio* of live traffic (a retry budget) bounds the multiplier; a circuit breaker cuts the loop outright. The cost is paid in latency and in abandoned work — a budget-limited client fails a request that a retry might have rescued, and decorrelated schedules make individual completion times less predictable.

The corpus already covers [why every retry needs a timeout and a bulkhead](/articles/microservices/2026-07-26-timeouts-retries-bulkheads/) and [why retried writes need idempotency keys](/articles/microservices/2026-07-30-idempotency-keys-safe-retries/). This article treats the arithmetic of backoff and the population dynamics that convert individually reasonable clients into a storm.

## Why undithered retries synchronize

A single client retrying three times imposes negligible load. The difficulty is correlation. When a service degrades, **every in-flight client observes the error within the same short interval**, so the retry schedule of the whole population is driven by one shared clock reading. With a fixed delay of 100 ms, the original demand spike replays at the same amplitude 100 ms later, indefinitely.

Plain exponential backoff — `sleep = base * 2^attempt` — separates the waves in time but does not disperse them. Clients that failed together remain in lockstep and arrive as **synchronized pulses at 1 s, 2 s, 4 s**. The server alternates between idle and saturated, and each pulse can destroy the recovery in progress: a partly refilled cache or a partly drained queue is knocked back to its starting state by an arrival burst that the steady-state capacity plan never anticipated.

The invariant that matters is not the *mean* rate of retries but the **peak instantaneous rate**. Deterministic backoff leaves the peak equal to the population size; the purpose of jitter is to spread that mass over the whole window so the peak approaches the mean.

## Full, equal, and decorrelated jitter

Marc Brooker's AWS Architecture Blog post *Exponential Backoff and Jitter* (2015) is the canonical treatment. It simulates contending clients against a capped exponential backoff and compares three randomisations:

- **Full jitter:** `sleep = random(0, min(cap, base * 2^attempt))`. The entire window is randomized, giving maximum spread.
- **Equal jitter:** `sleep = temp/2 + random(0, temp/2)` where `temp = min(cap, base * 2^attempt)`. At least half of the deterministic backoff is retained, "preventing very short sleeps."
- **Decorrelated jitter:** `sleep = min(cap, random(base, 3 * prev_sleep))`. No attempt counter is used; each sleep is drawn from a window that grows off the *previous* sleep.

Brooker measures two quantities: total calls made (a proxy for server load) and time to completion under contention. **Exponential backoff without jitter is clearly worst on both.** Full jitter and decorrelated jitter are the two winners, with **full jitter performing slightly less total work** and **decorrelated jitter finishing slightly faster**. Equal jitter fares worse in the same runs: work comparable to full jitter but longer completion times, which makes it hard to justify over full jitter on the published numbers.

Full jitter is the reasonable default, and is the variant the post recommends for the common case. Decorrelated jitter is the option when an attempt counter cannot be threaded through every call site, because its state is a single previous-sleep value.

### Implementation sketch (Scala)

```scala
import scala.util.Random
import scala.util.control.NonFatal
import scala.concurrent.duration.*

val Base = 50.millis
val Cap  = 10.seconds

/** Brooker's decorrelated jitter: the window grows off the last sleep, not an attempt counter. */
def decorrelated(prev: FiniteDuration): FiniteDuration =
  val hi = (3 * prev.toMillis).min(Cap.toMillis)
  Random.between(Base.toMillis, hi.max(Base.toMillis + 1)).millis

/** Token bucket in the Finagle shape: real requests deposit, retries withdraw. */
final class RetryBudget(percentCanRetry: Double, minRetriesPerSec: Int):
  private var tokens: Double = 0.0          // refilled separately at minRetriesPerSec
  def deposit(): Unit = synchronized { tokens += percentCanRetry }
  def tryWithdraw(): Boolean = synchronized {
    if tokens >= 1.0 then { tokens -= 1.0; true } else false
  }

def callWithRetries[A](op: () => A, budget: RetryBudget, deadline: Deadline): A =
  budget.deposit()
  var sleep = Base
  while true do
    try return op()
    catch case NonFatal(e) =>
      // Budget exhaustion, and an expired deadline, fail the request rather
      // than adding to the storm.
      if !budget.tryWithdraw() || !deadline.hasTimeLeft() then throw e
      sleep = decorrelated(sleep)
      Thread.sleep(sleep.toMillis)
  throw IllegalStateException("unreachable")
```

## Retry budgets bound the ratio, not the count

"At most three retries" reads as conservative but uses the wrong unit: it bounds each *request*, not the *fleet*. When the failure rate reaches 100%, three retries per client produce **four times the offered load precisely when available capacity is lowest**. Linkerd's documentation makes the same point about naive retry counts: a client that retries a failing request several times multiplies the traffic arriving at an already unhealthy service.

A **retry budget** bounds the ratio of retries to live traffic instead. Finagle's `RetryBudget` is the reference implementation: a token bucket in which each real request deposits a fraction of a token and each retry withdraws a whole one, parameterized by `ttl` (how long deposits live), `percentCanRetry`, and `minRetriesPerSec`. The documented default allows for about 20% of the total requests to be retried, on top of a floor of 10 retries per second. Linkerd offers a retry budget of the same shape. The Google SRE book reaches the same structure from the server side, describing a per-process retry budget of 60 retries per minute that fails the request outright once the allowance is exceeded — a distinction the chapter frames as the difference between a capacity-planning failure and a global cascading failure.

The behavioural difference under a total outage is the point: **a budget caps retry overhead at a bounded fraction of live traffic, whereas a per-request count multiplies load in proportion to the failure rate.**

## Amplification through layers

Budgets matter because **retries compose multiplicatively**. If a frontend, a backend and a data layer each retry three times, one user request can reach the bottom as `4 * 4 * 4 = 64` attempts. The SRE book states the general form: "a single request at the highest layer may produce a number of attempts as large as the *product* of the number of attempts at each layer."

The corresponding rule is to **retry at one layer only** — typically the lowest layer that can retry safely, or the edge, but not both — to **propagate deadlines downward** so that a request whose deadline has already expired is not retried at all, and to fail fast upward everywhere else.

## Retry storms sustain metastable failures

The HotOS '21 paper *Metastable Failures in Distributed Systems* (Bronson, Aghayev, Charapko, Zhu) supplies the formal shape. A **trigger** — a deploy, a cache flush, a load spike — pushes the system into a state where a **sustaining effect**, a feedback loop, holds goodput near zero **even after the trigger is removed**. The paper's running example is a retry loop: a database that comfortably serves 300 queries per second suffers a latency increase, timeouts fire, and retries hold offered load at 560 queries per second. "So long as latency is high, client queries will continue at 560 QPS due to retries. This will prevent the database from recovering."

The consequence for operators is that **root-causing the trigger does not end the incident**, because the trigger is no longer present. The intervention has to weaken the sustaining loop: budgets, load shedding, and reducing the work amplification of each failed request.

## Circuit breaking is the backstop

A [circuit breaker](/articles/microservices/2026-07-24-circuit-breakers-resilience4j/) is the last line of defence: once the observed error rate crosses a threshold it stops sending entirely, cutting the sustaining loop by force. It is cruder than a budget — **binary where a budget is proportional** — and it engages only after enough failures have been observed to move the threshold statistic. The layering that holds: jittered exponential backoff shapes *when* individual retries land, a retry budget bounds *how many* exist fleet-wide, and the breaker plus server-side [load shedding](/articles/microservices/2026-07-31-rate-limiting-load-shedding-token-bucket/) absorbs the remainder.

## Pitfalls

- **A shared cap with no jitter reintroduces the pulse.** Once every client's exponential backoff saturates at the cap, all sleeps are equal again and arrivals resynchronize at the cap interval.
- **Equal jitter costs completion time without saving work.** Brooker's simulation shows work comparable to full jitter but longer time to completion.
- **Decorrelated jitter seeded from zero collapses.** The next window is drawn from `random(base, 3 * prev_sleep)`; a previous sleep at or below `base/3` leaves the window degenerate, so the schedule stops growing.
- **Per-request retry counts hide fleet-level amplification.** Three retries appear bounded per caller and become a four-fold load multiplier when the failure rate reaches 100%.
- **Retrying at every layer multiplies rather than adds.** Three layers of three retries yield up to 64 attempts at the bottom for one user request.
- **Retries against an expired deadline consume capacity for a result nobody will read.** Without deadline propagation the lower layers keep working on requests the caller has already abandoned.
- **Fixing the trigger does not end a metastable outage.** By definition the sustaining loop maintains near-zero goodput after the trigger is gone, so only weakening the loop restores service.
- **A budget with no deposits blocks all retries.** The bucket is filled by live traffic; on a low-volume path the `minRetriesPerSec` floor, not `percentCanRetry`, determines whether any retry is permitted.
