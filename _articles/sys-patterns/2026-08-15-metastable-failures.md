---
title: "Metastable Failures: When the Outage Survives Its Trigger"
date: 2026-08-15
track: sys-patterns
summary: "Bronson et al. (HotOS '21) named the failure class where a trigger pushes a vulnerable system into a bad state that a sustaining feedback loop — retry storms, cache-miss storms, GC spirals — keeps alive after the trigger is gone. The OSDI '22 follow-up found 22 such failures across 11 organizations and metastability behind at least 4 of AWS's 15 major outages in a decade. Recovery means breaking the loop: shed load, cap retries, adaptive LIFO — not adding capacity."
reading_time: 7
tags: [metastable-failures, retry-storms, feedback-loops, load-shedding, reliability, incident-response]
sources:
  - title: "Bronson, Aghayev, Charapko & Zhu — Metastable Failures in Distributed Systems (HotOS '21)"
    url: "https://sigops.org/s/conferences/hotos/2021/papers/hotos21-s11-bronson.pdf"
  - title: "Huang et al. — Metastable Failures in the Wild (OSDI '22)"
    url: "https://www.usenix.org/system/files/osdi22-huang-lexiang.pdf"
  - title: "Marc Brooker — Metastability and Distributed Systems"
    url: "https://brooker.co.za/blog/2021/05/24/metastable.html"
  - title: "AWS Builders' Library — Timeouts, Retries, and Backoff with Jitter"
    url: "https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/"
---

**Gist.** A short disturbance — a database slowdown, a deploy, a cache wipe — can leave a distributed system pinned at near-zero goodput long after the disturbance ends, because the system's own response to overload generates more overload. Bronson, Aghayev, Charapko and Zhu (HotOS '21) call this a **metastable failure**: the outage has decoupled from its cause and is sustained by a feedback loop. Recovery requires driving the loop's amplification factor below one, which means deliberately serving less work — shedding load, capping retries, discarding expired queue entries — rather than adding capacity.

## Vulnerable, metastable, and the sustaining effect

The model has three states. A **stable** system returns to goodput after any bounded disturbance. A **vulnerable** system operates correctly — often at higher efficiency than a stable one — but sits within reach of a cliff. A **trigger** (load spike, deploy, brief dependency outage, cache wipe) moves it into the **metastable** state, where a **sustaining effect** — a feedback loop in which the response to overload creates further overload — holds the system down after the trigger has resolved. **That last clause is the defining test: remove the trigger, and the failure persists.** This distinguishes metastability from cascading failure, where removing the cause restores service, and from gray failure, where the fault itself is still present but partially observable.

The canonical sustaining loops:

- **Retry storms.** Clients time out and retry; retries multiply offered load precisely when capacity is scarcest; the extra load causes further timeouts. With a single retry per request, work amplification is 2x. The HotOS motivating example is a web tier whose retries sustain overload indefinitely after a **temporary database slowdown**. The arithmetic appears in [exponential backoff, jitter, and retry storms](/articles/microservices/2026-08-15-exponential-backoff-jitter-retry-storms); metastability is what that arithmetic produces once amplification crosses 1.0.
- **Cache-miss storms.** A look-aside cache sustaining a 90% hit rate implies the database is provisioned for 10% of true demand. Wiping or restarting the cache exposes the database to **10x its usual load**; it slows, requests time out, and **entries are never repopulated because the fill requests time out as well** — the hit rate stays at zero. The cache is no longer an optimization; it is load-bearing capacity. Related mechanics appear in [cache stampede and request coalescing](/articles/microservices/2026-08-10-cache-stampede-request-coalescing).
- **Garbage-collection (GC) spirals.** Slow responses grow the in-flight request queue; larger live heaps lengthen GC pauses; longer pauses slow responses further. Thread-pool exhaustion and lock convoys have the same shape.

Huang et al.'s OSDI '22 follow-up, **"Metastable Failures in the Wild,"** establishes the pattern empirically: **22 metastable failures across 11 organizations**, and **at least 4 of the 15 major AWS outages in the preceding decade**. The paper refines the model along two axes: triggers are either *load spikes* or *capacity drops*, and sustaining loops amplify either **workload** (retries, re-subscriptions, health-check floods) or **capacity degradation** (GC, cache hit-rate collapse, queue-induced timeout misses).

## Why additional capacity does not break the loop

The tipping point is visible in a fixed-point argument. Let `C` be served capacity, `d` steady demand, and `r` the number of retries per failed request. In-flight load at each step is `L = d + r·max(0, L − C)`. While `L ≤ C`, the system rests at the fixed point `L = d`. Above `C` the recurrence is linear with slope `r`, so for `r > 1` it has a second, **unstable** fixed point at `L* = (r·C − d)/(r − 1)`: a trigger that pushes `L` past `L*` makes the retry term grow faster than capacity absorbs it, and `L` runs away and stays away even after `d` returns to its pre-trigger value. **The feedback loop, not the raw demand, sets the load.** Adding servers raises `C`, and with it `L*` — it widens the trigger the system can absorb — but does nothing to `r`, so a large enough trigger crosses the new threshold as well, and capacity added *after* the crossing is consumed by retries rather than by demand.

Marc Brooker states the operational consequence directly: **the efficient system is the vulnerable one.** Caches, batching and high utilization all widen the gap between capacity assuming the optimization holds and capacity when it does not. Over-provisioning buys stability only up to the amplification factor the loop can reach.

### Implementation sketch (Scala)

The simulation below reproduces the fixed point: a five-step trigger, then demand returning to its original level.

```scala
final case class Step(inflight: Double, served: Double)

def simulate(
    capacity: Double,          // requests per tick the backend can serve
    demand: Double,            // steady client demand
    retries: Double,           // retries issued per failed request
    trigger: Double,           // elevated demand during the trigger
    triggerTicks: Int,
    ticks: Int
): LazyList[Step] =
  LazyList.iterate(Step(demand, demand) -> 0)((step, t) =>
    val offered = if t < triggerTicks then trigger else demand
    val served  = math.min(step.inflight, capacity)
    val failed  = step.inflight - served          // exceeded their deadline
    Step(offered + failed * retries, served) -> (t + 1)
  ).take(ticks).map(_._1)

// Retries per failure below one: no runaway fixed point exists, so inflight
// drains back to 800 once the trigger is withdrawn.
val recovers =
  simulate(capacity = 1000, demand = 800, retries = 0.5,
           trigger = 1400, triggerTicks = 5, ticks = 120)

// Above one: L* = (2*1000 - 800)/(2 - 1) = 1200, the trigger crosses it, and
// inflight grows without bound while served work is entirely retries.
val stuck =
  simulate(capacity = 1000, demand = 800, retries = 2.0,
           trigger = 1400, triggerTicks = 5, ticks = 120)
```

A retry budget is the same model with `retries` replaced by a per-client token bucket, so the term `failed * retries` is bounded by a fraction of total request volume rather than by the failure count.

## Breaking the loop

Recovery and prevention are the same move: reduce the amplification factor below one.

| Mechanism | Loop it breaks | Notes |
|---|---|---|
| Load shedding / admission control | queue growth → timeout → waste | reject at the front door; serve fewer requests, successfully |
| Retry budgets (a token-bucket fraction of request volume) | retry storm | a per-client token bucket bounds the integral; per-request retry counts do not |
| Exponential backoff + jitter | synchronized retry waves | necessary, not sufficient — budgets cap the total |
| Circuit breakers | repeated calls into a dead dependency | see [circuit breakers](/articles/microservices/2026-07-24-circuit-breakers-resilience4j) |
| Adaptive LIFO + timeout-aware dequeue | serving already-expired requests | first-in-first-out under overload wastes work at the back of the queue |
| Deadline propagation | downstream work for abandoned requests | drop work whose caller has already given up |

Two entries warrant expansion. **Last-in-first-out (LIFO) service under overload** — Facebook's "adaptive LIFO", paired with controlled-delay queue limits — rests on the observation that when a queue is long, the oldest entry is the one most likely to be past its client's deadline. First-in-first-out ordering therefore converts the backlog into work that consumes capacity and produces nothing; serving newest-first converts part of it back into goodput. **Admission control as a recovery tool** follows from the OSDI observations: operators escape metastability by throttling *below* normal demand, serving a reduced fraction of traffic until caches refill and queues drain, then increasing the limit incrementally. The mechanisms are covered in [rate limiting and load shedding](/articles/microservices/2026-07-31-rate-limiting-load-shedding-token-bucket); metastability is the argument for exposing them as an operator-controlled switch rather than an automatic-only policy.

The design-review question the framework yields: for every optimization the system leans on — cache hit rate, connection reuse, batching — what is the load profile on the day it delivers zero benefit, and does any response to overload anywhere in the stack increase load? Each affirmative answer is a stored outage awaiting its trigger.

## Pitfalls

- **Capacity added during the incident is absorbed by the loop.** Goodput stays near zero after a fleet doubles because the new capacity is spent on the retry backlog it inherits; the amplification factor is unchanged by server count.
- **Rebooting the fleet re-arms the trigger.** Restarts empty caches and connection pools, so the recovering system faces the cache-miss storm again from a cold start.
- **Backoff and jitter alone do not bound the retry integral.** They de-synchronize retry waves but leave the retries-per-failure ratio unchanged, so amplification above one still holds the system down.
- **A high steady-state cache hit rate hides the true database sizing.** A backend provisioned against a 90% hit rate has no headroom for the 10x demand it sees when the cache is empty.
- **First-in-first-out queues under overload spend capacity on expired requests.** Every dequeued entry whose client deadline has passed is served into a closed connection, so measured server work stays high while goodput stays at zero.
- **Health checks and re-subscriptions are workload amplifiers.** Failing probes that trigger additional probing or mass client re-registration form a sustaining loop with no user request behind it.
