---
title: "Timeouts, retries, and bulkheads: the three habits that stop a cascade"
date: 2026-07-26
track: microservices
summary: "A circuit breaker reacts after a dependency is already sick. Timeouts, jittered backoff, and bulkheads are the upstream habits that decide whether a slow call stays a local annoyance or becomes a fleet-wide retry storm."
reading_time: 6
tags: [timeouts, retries, backoff, jitter, bulkhead, resiliency, resilience4j]
sources:
  - title: "Marc Brooker (AWS) — Timeouts, retries, and backoff with jitter"
    url: "https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/"
  - title: "AWS Architecture Blog — Exponential Backoff and Jitter"
    url: "https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/"
  - title: "Sam Newman, Building Microservices (2nd ed.) — Resiliency (Ch. 12)"
    url: "https://www.oreilly.com/library/view/building-microservices-2nd/9781492034018/"
  - title: "Michael T. Nygard, Release It! (2nd ed.)"
    url: "https://pragprog.com/titles/mnee2/release-it-second-edition/"
  - title: "Resilience4j — Bulkhead docs"
    url: "https://resilience4j.readme.io/docs/bulkhead"
---

The circuit breaker article on this journal ends with a hint: breakers, timeouts, and bulkheads are "the three legs Newman leans on together." A breaker is a *reaction* — it trips only after a dependency has already proven itself sick. Timeouts, retries, and bulkheads are what decide how much damage happens before that trip, and whether your own service survives long enough to notice. Nygard's *Release It!* built an entire vocabulary of stability antipatterns around exactly this gap, and it still holds up: most outages aren't caused by a dependency dying, they're caused by a dependency getting *slow* and everyone upstream handling that badly.

## Timeouts: connect vs read, and why "no timeout" is a timeout of infinity

Every remote call has at least two timeout knobs, and conflating them is the first mistake:

- **Connect timeout** — how long you'll wait to establish a TCP/TLS session. This should be short (hundreds of milliseconds) because a healthy host answers a SYN almost immediately; a slow connect usually means the host is unreachable or overloaded, not "about to respond."
- **Read (socket) timeout** — how long you'll wait for the response once the request is sent. This needs to reflect the *dependency's* actual latency distribution, not a guess.

Marc Brooker's AWS Builders' Library article is blunt about the default: a client with no timeout configured has effectively chosen a timeout of infinity, and that's a design decision, not an oversight. His guidance is to set timeouts from the downstream's own latency percentiles — often p99.9 — with a deliberate, quantified rate of "false" timeouts you're willing to accept, rather than a round number that feels safe. Newman makes the same point in *Building Microservices*: every network call is a potential hang, so pick a sane default timeout for *all* outbound calls, then override per-dependency where the latency profile differs.

| Knob | Typical range | Set based on |
|---|---|---|
| Connect timeout | 100–500 ms | Network RTT to the dependency, not its business logic |
| Read timeout | p99–p99.9 latency + margin | The dependency's *measured* latency histogram |
| Per-call deadline (end-to-end) | Sum of hop budgets | The caller's own SLA, propagated downstream |

## Retries: why naive retries cause the outage they're meant to prevent

A retry is a bet that the failure was transient. Nygard's core warning in *Release It!* is that this bet becomes a liability the moment a dependency is *overloaded* rather than merely blipping: retrying against an overloaded service adds load to the thing that's already drowning, producing more failures, which triggers more retries. This is the mechanism behind a **retry storm**, and it's one path into what the resilience literature calls a **metastable failure** — a state where the system won't recover on its own even after the original trigger (a deploy, a network blip, a GC pause) is gone, because the retry traffic itself has become the sustaining load.

Brooker's article adds a multiplicative danger easy to miss in a service mesh: if five layers of the call graph each retry three times independently, a single failure at the bottom can fan out to 3^5 = 243 calls. His recommendation is to retry at exactly one layer of the stack — usually the layer closest to the caller who can make a sensible fallback decision — and make every other layer fail fast.

The fix for both problems is the same shape: **exponential backoff with jitter**, plus a **retry budget** that caps total retry volume as a fraction of primary traffic (a common ratio is capping retries at 10% of the request rate, tracked with a token bucket). Backoff without jitter is a trap — synchronized clients back off in lockstep and re-collide on the next attempt. AWS's own comparison of jitter strategies (full jitter, equal jitter, decorrelated jitter) found that any jittered strategy cuts total client work roughly in half versus plain exponential backoff, and "full jitter" is the simplest to implement:

```python
import random
import time

def call_with_retry(fn, max_attempts=5, base=0.1, cap=10.0, budget=None):
    for attempt in range(max_attempts):
        if budget is not None and not budget.take():
            raise RetriesExhausted("retry budget depleted")
        try:
            return fn()
        except TransientError:
            if attempt == max_attempts - 1:
                raise
            # Full jitter: random(0, min(cap, base * 2**attempt))
            sleep_for = random.uniform(0, min(cap, base * 2 ** attempt))
            time.sleep(sleep_for)
```

The `budget` object is not optional decoration — it's what stops a healthy-looking retry policy from turning into a storm the moment error rates spike fleet-wide.

## Idempotency: the prerequisite nobody budgets time for

None of this is safe unless the operation being retried is **idempotent** — calling it twice has the same effect as calling it once. A payment charge, an email send, or an "increment inventory" call are not naturally idempotent, and a retry after a *timeout* (as opposed to an explicit error) is the dangerous case: you genuinely don't know if the first attempt succeeded server-side. Newman and Nygard both treat this as a design-time concern, not a client-side patch — the standard fix is a client-supplied **idempotency key** per logical operation, with the server deduplicating on that key so a retried request either no-ops or returns the original result. If you can't make an operation idempotent, you can't safely retry it; the honest alternative is surfacing the failure and letting a human or a saga compensating action handle it.

## Bulkheads: stop one dependency's queue from eating everyone else's threads

Even with sane timeouts and disciplined retries, a degraded dependency still occupies resources for the duration of every in-flight call. Nygard's bulkhead pattern — named for the watertight compartments that keep a hull breach from sinking the whole ship — isolates the resource pool (threads, connections, semaphore permits) used for each dependency, so exhausting the pool for a sick `payments` service can't starve the threads a healthy `catalog` call needs.

Resilience4j ships two bulkhead flavors: a `SemaphoreBulkhead` that caps concurrent callers using a permit count, and a `ThreadPoolBulkhead` that gives a dependency its own bounded thread pool and queue.

```java
ThreadPoolBulkheadConfig config = ThreadPoolBulkheadConfig.custom()
    .coreThreadPoolSize(8)
    .maxThreadPoolSize(12)
    .queueCapacity(20)          // bounded — reject, don't queue forever
    .keepAliveDuration(Duration.ofMillis(500))
    .build();

ThreadPoolBulkhead paymentsBulkhead =
    ThreadPoolBulkheadRegistry.of(config).bulkhead("payments");

Supplier<CompletionStage<Receipt>> isolated =
    ThreadPoolBulkhead.decorateSupplier(paymentsBulkhead, () -> paymentsClient.charge(order));
```

Give every external dependency its own named bulkhead sized to what it can actually sustain, keep the queue bounded (an unbounded queue just delays the same exhaustion), and pair it with the timeout and backoff work above — a bulkhead limits the *blast radius* of a slow dependency, it doesn't make the calls themselves faster.

## Where this leaves the breaker

Circuit breakers still matter — they're what turns "keep timing out slowly" into "fail instantly for ten seconds." But a breaker only sees clean signal if the calls feeding it already have sane timeouts, and it only protects the caller's own thread pool if that pool is bulkheaded off from other dependencies. Timeouts, jittered retries with a budget, idempotency, and bulkheads are the groundwork; the breaker is the alarm bell sitting on top of it.

**Try next:** take the retry snippet above, point it at a dependency you control, and inject a 30% failure rate with no backoff cap — watch client-side CPU and connection counts climb — then add the jitter and budget back in and compare.
