---
title: "Exponential Backoff, Jitter, and the Anatomy of a Retry Storm"
date: 2026-08-15
track: microservices
summary: "Exponential backoff without jitter turns your clients into a synchronized battering ram; Marc Brooker's AWS analysis shows why, and which jitter variant to pick. Then the uglier math: retries multiplying through layers, retry budgets as the cap, and why a retry storm can hold a system down after the original trigger is gone."
reading_time: 6
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

The corpus already covers [why every retry needs a timeout and a bulkhead](/articles/microservices/2026-07-26-timeouts-retries-bulkheads/) and [why retried writes need idempotency keys](/articles/microservices/2026-07-30-idempotency-keys-safe-retries/). This article is about the part interviewers push on next: the *math* of backoff, and the population dynamics that turn polite individual clients into a storm.

## Why naive retries synchronize

A single client retrying three times is harmless. The problem is that failures are correlated: when a service blips, *every* client sees the error in the same instant, and if they all retry after a fixed 100 ms, the original spike replays itself — same size, 100 ms later. Plain exponential backoff (`sleep = base * 2^attempt`) spreads the waves out in time but does nothing to break them up: clients that failed together stay in lockstep, arriving in synchronized pulses at 1s, 2s, 4s. The server oscillates between idle and overloaded, and each pulse can knock over the recovery in progress.

## Full, equal, and decorrelated jitter

Marc Brooker's AWS Architecture Blog post *Exponential Backoff and Jitter* (2015) is the canonical treatment. It simulates contending clients against a capped exponential backoff and compares three ways of adding randomness:

- **Full jitter:** `sleep = random(0, min(cap, base * 2^attempt))`. Maximum spread — the entire window is randomized.
- **Equal jitter:** `sleep = temp/2 + random(0, temp/2)` where `temp = min(cap, base * 2^attempt)`. Keeps at least half the deterministic backoff, "preventing very short sleeps."
- **Decorrelated jitter:** `sleep = min(cap, random(base, 3 * prev_sleep))`. No attempt counter at all — each sleep is drawn from a window that grows off the *previous* sleep.

His results, measured as total calls made (server load) and time to completion under contention: no-jitter exponential backoff is clearly worst; full jitter and decorrelated jitter are the winners, with full jitter doing slightly less work and decorrelated jitter finishing slightly faster; equal jitter turns out to be a bad compromise — similar work to full jitter but much longer completion times, so there's little reason to pick it. Default to **full jitter** (it's what AWS SDKs ship); reach for decorrelated when you don't want to track attempt counts across call sites.

```python
import random

BASE, CAP = 0.05, 10.0   # seconds

def decorrelated_jitter(prev_sleep: float) -> float:
    """Brooker's decorrelated jitter: window grows off the last sleep."""
    return min(CAP, random.uniform(BASE, 3 * prev_sleep))

def call_with_retries(op, budget, max_elapsed=30.0):
    sleep, elapsed = BASE, 0.0
    while True:
        try:
            return op()
        except RetryableError:
            if elapsed >= max_elapsed or not budget.try_withdraw():
                raise                      # budget empty: fail, don't storm
            sleep = decorrelated_jitter(sleep)
            time.sleep(sleep)
            elapsed += sleep

class RetryBudget:
    """Token bucket: deposit per request, withdraw per retry (Finagle-style)."""
    def __init__(self, percent_can_retry=0.2, min_retries_per_sec=10):
        self.ratio, self.floor = percent_can_retry, min_retries_per_sec
        self.tokens = 0.0                  # plus a per-second refill of `floor`
    def deposit(self):      self.tokens += self.ratio
    def try_withdraw(self):
        if self.tokens >= 1: self.tokens -= 1; return True
        return False
```

## Retry budgets: cap the ratio, not the count

"Max 3 retries" sounds conservative but is the wrong unit: it bounds each *request*, not the *fleet*. When 100% of requests fail, 3 retries per client means 4x offered load exactly when capacity is lowest — Linkerd's docs call this out directly: with three retries "this can quadruple the number of requests being sent."

A **retry budget** bounds the ratio of retries to real traffic instead. Finagle's `RetryBudget` is the reference implementation: a token bucket where each real request deposits a fraction of a token and each retry withdraws one, parameterized by `ttl` (how long deposits live), `percentCanRetry`, and `minRetriesPerSec`; the default "allows for about 20% of the total requests to be immediately retried on top of 10 retries per second." Linkerd inherited the idea as its default retry mechanism. The Google SRE book arrives at the same place from the server side: a per-process budget ("only allow 60 retries per minute... if exceeded, don't retry; just fail") can be "the difference between a capacity planning failure... and a global cascading failure." During a real outage a budget degrades gracefully: retries cost you a bounded 10–20% overhead, instead of a load multiplier that scales with the failure rate.

## Amplification through layers

Budgets matter most because retries *compose multiplicatively*. If frontend, backend, and data layer each retry 3 times, one user request can become `4 * 4 * 4 = 64` attempts at the bottom — the SRE book's exact example: "a single request at the highest layer may produce a number of attempts as large as the *product* of the number of attempts at each layer." The standard rule: retry at **one** layer (usually the lowest one that can retry safely, or the edge, but not both), propagate deadlines downward so a doomed request isn't retried against an expired budget, and let everything else fail fast upward.

## Retry storms sustain metastable failures

The HotOS '21 paper *Metastable Failures in Distributed Systems* (Bronson, Aghayev, Charapko, Zhu) gives the storm its formal shape: a **trigger** (deploy, cache flush, load spike) pushes the system into a state where a **sustaining effect** — a feedback loop — keeps goodput near zero *even after the trigger is removed*. Their running example is exactly a retry loop: a database that comfortably serves 300 QPS gets a latency blip, timeouts fire, and retries hold offered load at 560 QPS; "so long as latency is high, client queries will continue at 560 QPS due to retries. This will prevent the database from recovering." Nothing is broken anymore — the retries *are* the outage. The paper's uncomfortable conclusion is that root-causing the trigger misses the point; the fix is weakening the sustaining loop: budgets, load shedding, and shrinking the work amplification of each failed request.

## Circuit breaking is the backstop, not the plan

A [circuit breaker](/articles/microservices/2026-07-24-circuit-breakers-resilience4j/) is the last line: when error rates cross a threshold it stops sending entirely, cutting the sustaining loop by brute force. It's cruder than a budget — binary where a budget is proportional — and it reacts only after the damage shows. The layering that works in practice: jittered exponential backoff shapes *when* individual retries land, a retry budget bounds *how many* exist fleet-wide, and the breaker (plus server-side [load shedding](/articles/microservices/2026-07-31-rate-limiting-load-shedding-token-bucket/)) catches whatever gets through.

**Try next:** simulate 1,000 clients against a server with capacity 100 req/s; compare offered load over time for fixed-delay retries, no-jitter exponential, and full jitter with a 20% retry budget — then remove the trigger mid-run and see which configurations recover.
