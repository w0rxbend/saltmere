---
title: "Metastable Failures: When the Outage Survives Its Trigger"
date: 2026-08-15
track: sys-patterns
summary: "Bronson et al. (HotOS '21) named the failure class where a trigger pushes a vulnerable system into a bad state that a sustaining feedback loop — retry storms, cache-miss storms, GC spirals — keeps alive after the trigger is gone. The OSDI '22 follow-up found 22 such failures across 11 organizations and metastability behind at least 4 of AWS's 15 major outages in a decade. Recovery means breaking the loop: shed load, cap retries, adaptive LIFO — not adding capacity."
reading_time: 6
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

The database blips for ninety seconds. The blip ends — and the site stays down for six hours anyway, throughput pinned near zero while every dashboard shows servers busy doing... something. Rebooting fleets doesn't help. Doubling capacity doesn't help. This isn't cascading failure or gray failure; it's what Bronson, Aghayev, Charapko and Zhu named a **metastable failure** (HotOS '21): the outage has decoupled from its cause, and the system is now feeding on itself.

## Vulnerable, metastable, and the sustaining effect

The model has three states. A **stable** system returns to goodput after any bounded disturbance. A **vulnerable** system runs fine — often *more* efficiently than a stable one — but sits within reach of a cliff. A **trigger** (load spike, deploy, brief dependency outage, cache wipe) tips it into the **metastable** state, where a **sustaining effect** — a feedback loop in which the *response to overload creates more overload* — holds the system down even after the trigger fully resolves. That last clause is the defining test: remove the trigger, and the failure persists.

The canonical loops:

- **Retry storms.** Clients time out and retry; retries multiply offered load exactly when capacity is scarcest; extra load causes more timeouts. With one retry, work amplification is 2x — the HotOS paper's motivating example shows a web tier whose retries sustain overload indefinitely after a 10-minute database slowdown. We covered the arithmetic in [exponential backoff, jitter, and retry storms](/articles/microservices/2026-08-15-exponential-backoff-jitter-retry-storms); metastability is what happens when that arithmetic crosses 1.0.
- **Cache-miss storms.** A look-aside cache at 90%+ hit rate means the database is sized for 10% of true demand. Wipe the cache (or restart it) and the database sees 10x load, slows, requests time out, *entries never get repopulated because the fills are timing out too* — hit rate stays at zero. The cache stopped being an optimization years ago; it became load-bearing capacity. (Related mechanics: [cache stampede and request coalescing](/articles/microservices/2026-08-10-cache-stampede-request-coalescing).)
- **GC spirals.** Slow responses grow in-flight request queues; bigger heaps mean longer GC pauses; longer pauses mean slower responses. Same shape with thread-pool exhaustion and lock convoys.

Huang et al.'s OSDI '22 follow-up, **"Metastable Failures in the Wild,"** showed this is a pattern, not an anecdote: **22 metastable failures across 11 organizations**, from hyperscalers down, and **at least 4 of the 15 major AWS outages in the preceding decade** — including the December 2021 us-east-1 event, where retries sustained congestion on an internal network long after the triggering surge. They refine the model usefully: triggers come as *load-spike* or *capacity-drop*, and sustaining loops amplify either **workload** (retries, re-subscriptions, health-check floods) or **capacity degradation** (GC, cache hit-rate collapse, queue-induced timeout misses).

## Why adding capacity doesn't save you

Here's the tipping point as a loop you can run:

```python
capacity   = 1000        # req/s the backend can serve
offered    = 800         # steady client demand, req/s
timeout_ok = lambda inflight: inflight < capacity   # served within deadline

inflight = offered
for t in range(120):
    served  = min(inflight, capacity)
    failed  = inflight - served          # timed out this tick
    retries = failed * 1.0               # each failure retried once
    inflight = offered + retries
    print(t, inflight, served)

# trigger: set offered = 1100 for 5 ticks, then back to 800.
# inflight jumps the cliff: failed>0 -> retries -> inflight stays >capacity
# forever, though demand returned to 80% utilization. Goodput never recovers.
```

Once `offered + retries > capacity`, failures beget retries beget failures — a fixed point above capacity. Now note what adding 25% more servers does: nothing, because the amplified load is `2 × offered` and still exceeds it. The feedback loop, not the raw demand, sets the load. Marc Brooker's framing is the operational headline: **the efficient system is the vulnerable one.** Caches, batching, and high utilization all widen the gap between "capacity assuming the optimization holds" and "capacity when it doesn't" — you can buy stability with over-provisioning, but you're paying for capacity the sustaining loop will still outrun if the amplification factor is high enough.

## Breaking the loop

Recovery and prevention are the same move: make the amplification factor less than one.

| Mechanism | Loop it breaks | Notes |
|---|---|---|
| Load shedding / admission control | queue growth → timeout → waste | reject early at the front door; serve fewer, successfully |
| Retry budgets (e.g. 10% of requests) | retry storm | per-client token bucket beats per-request retry counts |
| Exponential backoff + jitter | synchronized retry waves | necessary, not sufficient — budgets cap the integral |
| Circuit breakers | repeated calls into a dead dependency | see [circuit breakers](/articles/microservices/2026-07-24-circuit-breakers-resilience4j) |
| Adaptive LIFO + timeout-aware dequeue | serving already-expired requests | FIFO under overload does 100% wasted work at the back |
| Deadline propagation | downstream work for abandoned requests | drop work whose caller already gave up |

Two deserve emphasis. **LIFO under overload** (Facebook's "adaptive LIFO," paired with controlled-delay queue limits): when a queue is long, the oldest request is the one most likely past its client's deadline, so FIFO turns the whole backlog into dead work that still consumes capacity — serving newest-first converts some of that into goodput and starves the loop. And **admission control as recovery tool**: the OSDI paper observes operators escape metastability by *throttling below normal demand* — deliberately serving, say, 50% of traffic until caches refill and queues drain, then ratcheting up. Counterintuitive in an incident ("we're down and you want to reject more?"), which is exactly why the runbook should be written before 3 a.m. The [rate limiting and load shedding](/articles/microservices/2026-07-31-rate-limiting-load-shedding-token-bucket) piece covers the mechanisms; metastability is the argument for wiring them to a big red switch.

The design-review question this framework hands you: for each optimization the system leans on (cache hit rate, connection reuse, batching), what happens the day it delivers zero — and is there any response to overload anywhere in the stack that *increases* load? Every "yes" is a stored outage waiting for its trigger.

**Try next:** extend the simulation above into a two-parameter sweep — retry count r in {0,1,2,3} and utilization u in {0.5..0.95} — and plot the region where goodput fails to recover after a 5-tick trigger. Then add a 10% retry budget and watch the metastable region collapse to nearly nothing.
