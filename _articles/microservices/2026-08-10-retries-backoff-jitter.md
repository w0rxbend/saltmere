---
title: "Retries done right: exponential backoff, jitter, and not starting a storm"
date: 2026-08-10
track: microservices
summary: "A naive retry loop is a load amplifier: the moment a dependency slows down, every client piles on and holds it there. Exponential backoff spreads the attempts, jitter de-synchronizes them, and a retry budget caps the blast radius so a transient blip can't metastasize into a self-sustaining outage."
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

The [timeouts-and-bulkheads article](/articles/microservices/2026-07-26-timeouts-retries-bulkheads) on this journal makes the case that a retry is a *bet the failure was transient*. This one goes deeper on the two knobs that decide whether that bet pays off or backfires: how you space attempts (**backoff + jitter**) and how many attempts the whole fleet is collectively allowed to make (**a retry budget**). Get those wrong and retries become the outage.

## Why naive retries amplify an outage

Picture a service running near capacity. A deploy, a GC pause, or a brief network blip pushes it over the edge and a slice of requests start failing. Every client retries immediately. That retry traffic is *added on top* of the primary request rate, so the offered load jumps — often to 2x or 3x — precisely when the service has the least headroom. More requests fail, which triggers more retries, which raises load further.

This is a **retry storm**, and it's one of the cleanest routes into a **metastable failure**: a state where the system will not recover on its own even after the original trigger is gone, because the retry traffic has *become* the sustaining load. You can remove the deploy, fix the network, end the GC pause — and the service stays down because clients are still hammering it. The only cures are capacity you may not have or shedding the retries themselves. Marc Brooker's AWS Builders' Library article is blunt about the framing: retries are "selfish" — a client retrying is optimizing for itself at the expense of the shared backend, so the system as a whole needs a way to bound them.

## Exponential backoff, and why it isn't enough

The first fix is to stop retrying immediately. **Capped exponential backoff** doubles the wait after each attempt, up to a ceiling:

```
sleep = min(cap, base * 2 ** attempt)
```

With `base = 100 ms` and `cap = 20 s`, attempts wait ~100 ms, 200 ms, 400 ms, 800 ms… This spreads a single client's attempts out and gives the backend time to drain its queue.

But backoff alone has a subtle failure mode: **synchronization**. If a shared dependency blips and knocks out a thousand clients at the same instant, they *all* compute the same `base * 2 ** attempt` and all wake up at the same moment to retry together. The load arrives in sharp, synchronized spikes at 100 ms, then 200 ms, then 400 ms — each spike large enough to re-trip the very overload you're backing off from. Doubling the interval doesn't help if everyone doubles in lockstep. As Brooker puts it in his [own blog](https://brooker.co.za/blog/2015/03/21/backoff.html), the goal isn't just to wait longer, it's to *spread the calls out in time*.

## Jitter: the randomness that de-synchronizes clients

Jitter adds randomness to each client's wait so the spikes flatten into a smooth, roughly uniform arrival rate. AWS's "Exponential Backoff And Jitter" analysis benchmarked three variants against plain capped backoff. The formulas, verbatim:

**Full Jitter** — pick a wait uniformly between zero and the backoff ceiling:

```
sleep = random(0, min(cap, base * 2 ** attempt))
```

**Equal Jitter** — keep half the interval fixed, randomize the other half:

```
temp  = min(cap, base * 2 ** attempt)
sleep = temp / 2 + random(0, temp / 2)
```

**Decorrelated Jitter** — grow the window off the *previous* sleep rather than the attempt count:

```
sleep = min(cap, random(base, sleep * 3))   # seed sleep = base
```

AWS's simulation measured two things: total client work (number of calls to complete all requests) and total time. Every jittered strategy cut client work roughly in half versus un-jittered backoff. **Full Jitter** came out as the recommended default: it produces the lowest and most even server load, needs no extra state, and the small amount of extra completion time it costs versus decorrelated jitter is a good trade for its simplicity. Equal jitter's fixed half means clients still cluster at the *start* of each window — better than nothing, worse than full. Decorrelated jitter is competitive but requires carrying the previous `sleep` between attempts. Unless you have a measured reason otherwise, **use full jitter.**

Here's full jitter with the guardrails a reviewer will look for — a max attempt cap, a total deadline, retryable-error gating, and `Retry-After` support:

```python
import random, time

def call_with_retry(fn, is_retryable, max_attempts=5,
                    base=0.1, cap=20.0, deadline=None, budget=None):
    deadline = deadline or (time.monotonic() + 30.0)
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            # 1. Only retry things that are safe AND worth retrying.
            if not is_retryable(e) or attempt == max_attempts - 1:
                raise
            # 2. A depleted retry budget means the backend is fleet-wide sick.
            if budget is not None and not budget.take():
                raise
            # 3. Server told us how long to wait? Obey it.
            retry_after = getattr(e, "retry_after", None)
            if retry_after is not None:
                sleep = retry_after
            else:  # full jitter
                sleep = random.uniform(0, min(cap, base * 2 ** attempt))
            # 4. Never sleep past the caller's deadline.
            if time.monotonic() + sleep > deadline:
                raise
            time.sleep(sleep)
```

## Guardrails interviewers want to hear

Backoff and jitter shape *when* a single client retries. They do nothing to bound *how many* retries the fleet issues in aggregate. Four guardrails close that gap.

**Only retry idempotent, retryable operations.** A retry can duplicate a side effect if the first attempt actually succeeded but the response was lost. Retrying a `GET` is free; retrying a `POST /charge` can double-bill. Make writes safe with [idempotency keys](/articles/microservices/2026-07-30-idempotency-keys-safe-retries) before you enable retries on them. And gate on the *error*: retry timeouts, connection failures, `503`, and `429`; never retry a `400` or `403` — the answer won't change.

**Cap attempts and a total deadline.** Two-to-three retries recover the overwhelming majority of transient faults; beyond that you're mostly adding load. Pair the attempt cap with an end-to-end [deadline](/articles/microservices/2026-08-10-timeouts-deadlines-propagation) so a chain of backoffs can't blow the caller's SLA — and so retries don't fire against a request the caller already abandoned.

**Retry budget / token bucket.** This is the single most important storm defense. Cap retries to a small fraction of primary traffic — a token bucket that only permits, say, retries up to ~10-20% of the request rate. When a dependency fails fleet-wide, error rates spike, the budget drains in seconds, and further retries are *refused* — the fleet degrades to primary traffic only instead of amplifying. Both major RPC stacks ship this:

- **Finagle** defaults to allowing 20% of requests to be retried on top of a floor of 10 retries/second, tracked in a token bucket whose credits expire after 10 seconds.
- **gRPC** `retryThrottling` runs a per-connection token bucket: each failure decrements the count by 1, each success adds back `tokenRatio` (e.g. `maxTokens: 10, tokenRatio: 0.1`), and retries pause once the count drops below `maxTokens / 2`.

A minimal budget guard the snippet above plugs into:

```python
import time

class RetryBudget:
    """Token bucket: cap retries to `ratio` of request throughput."""
    def __init__(self, ratio=0.1, min_per_sec=10, ttl=10.0):
        self.ratio, self.min_per_sec, self.ttl = ratio, min_per_sec, ttl
        self.tokens, self.last = float(min_per_sec * ttl), time.monotonic()

    def _decay(self):
        now = time.monotonic()
        # Tokens live for `ttl`; drain the stale ones.
        self.tokens *= max(0.0, 1 - (now - self.last) / self.ttl)
        self.last = now

    def deposit(self):          # call on every primary request
        self._decay()
        self.tokens += self.ratio

    def take(self):             # call before each retry
        self._decay()
        floor = self.min_per_sec * self.ttl * self.ratio
        if self.tokens >= 1 + floor:
            self.tokens -= 1
            return True
        return False            # budget depleted -> refuse the retry
```

**Circuit breakers.** A [circuit breaker](/articles/microservices/2026-07-24-circuit-breakers-resilience4j) trips after sustained failure and fails fast for a cooldown, so retries can't even be attempted against a backend that's clearly down. Budget and breaker are complementary: the budget bounds retry *volume* under partial failure; the breaker halts calls entirely under total failure.

**Respect `Retry-After`.** When a server returns `429` or `503` with a `Retry-After` header, it's telling you exactly when to come back. Honor it over your computed backoff — it's the backend's own admission-control signal, and ignoring it is how you turn load-shedding into a storm.

## The mental model

Backoff spreads one client's attempts. Jitter de-synchronizes the fleet so those attempts don't re-collide. The budget caps total retry volume so a fleet-wide failure can't amplify. The breaker cuts calls entirely when the backend is down. Idempotency makes any of it safe to attempt. Skip any one layer and a transient blip has a path to a self-sustaining outage.

**Try next:** take the `call_with_retry` + `RetryBudget` snippets, point them at a toy dependency, and inject a 40% failure rate. Watch offered load and client CPU with the budget disabled, then re-enable it and compare — the storm should flatten the moment tokens run out.
