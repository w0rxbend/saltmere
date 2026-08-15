---
title: "Timeouts, retries, and bulkheads: the habits that stop a cascade"
date: 2026-07-26
track: microservices
summary: "A circuit breaker reacts after a dependency is already sick. Timeouts, jittered backoff, and bulkheads are the upstream mechanisms that decide whether a slow call stays local or becomes a fleet-wide retry storm."
reading_time: 7
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

**Gist.** Most service outages begin not with a dependency dying but with a dependency becoming slow, and with every caller upstream handling that slowness badly: waiting without bound, retrying without restraint, and sharing one thread pool across all downstreams. Timeouts bound the wait, exponential backoff with jitter and a retry budget bound the amplification, and bulkheads bound the resource blast radius. Each mechanism buys that bound by trading away work that might have succeeded — a timeout aborts requests that would eventually have returned, a budget refuses retries that would have worked, and a bulkhead rejects calls while capacity sits idle elsewhere.

A circuit breaker is a *reaction*: it trips only after a dependency has already proven itself sick. The mechanisms below determine how much damage accrues before that trip, and whether the calling service remains healthy enough to observe it. Nygard's *Release It!* catalogues the stability antipatterns that occupy this gap — among them unbounded result sets, blocked threads, and cascading failure.

## Timeouts: connect versus read, and the infinite default

Every remote call has at least two independent timeout parameters, and conflating them is the first error:

- **Connect timeout** — the bound on establishing a Transmission Control Protocol (TCP) session and, where applicable, the Transport Layer Security (TLS) handshake. A healthy host acknowledges a SYN within roughly one network round-trip time (RTT), so a connect that exceeds a few hundred milliseconds indicates an unreachable or saturated host rather than one about to respond.
- **Read (socket) timeout** — the bound on waiting for the response after the request has been written. This must reflect the dependency's **measured latency distribution**, not an estimate.

Brooker's AWS Builders' Library article makes the consequence of the default explicit: **a client that configures no timeout has, in effect, chosen a timeout of infinity**. Its guidance is to derive the value from the downstream's measured latency distribution — a high percentile such as p99.9 — accepting a deliberate rate of spurious timeouts, rather than picking a round number. Newman makes the parallel point in *Building Microservices*: every network call is a potential hang, so a default timeout applies to all outbound calls and is overridden per dependency where the latency profile differs.

The invariant that makes this compose is the **deadline**: a caller's remaining budget, propagated downstream, so that no hop spends time on a request whose originator has already given up.

| Parameter | Typical range | Derived from |
|---|---|---|
| Connect timeout | A small multiple of RTT | Network RTT to the dependency, not its business logic |
| Read timeout | p99–p99.9 latency plus margin | The dependency's measured latency histogram |
| Per-call deadline (end-to-end) | Sum of hop budgets | The caller's own service-level agreement, propagated downstream |

## Retries: the amplification that causes the outage it was meant to prevent

A retry is a bet that a failure was transient. Nygard's warning is that the bet inverts the moment the dependency is *overloaded* rather than blipping: retrying against a saturated service adds load to the thing already failing, which produces more failures, which triggers more retries. That positive feedback loop is a **retry storm**, and it is one path into a **metastable failure** — a state in which the system does not recover after the original trigger (a deployment, a network blip, a garbage-collection pause) has passed, because the retry traffic has itself become the sustaining load.

A second hazard is multiplicative and specific to deep call graphs: retries compose by multiplication, so **five layers each attempting three times independently turn one failure at the bottom into 3^5 = 243 calls**. Brooker's recommendation is to retry at exactly one layer — normally the layer closest to the caller that can make a meaningful fallback decision — and to have every other layer fail fast.

Both hazards are addressed by the same shape: **exponential backoff with jitter**, plus a **retry budget** capping total retry volume as a fraction of primary traffic — for example one retry admitted per ten requests, tracked with a token bucket. Backoff without jitter is the trap: clients that failed together back off in lockstep and re-collide on the next attempt, reproducing the same thundering herd at each doubling. AWS's simulation comparing jitter strategies (full jitter, equal jitter, decorrelated jitter) reports that **every jittered strategy reduces total client work and contention substantially relative to plain exponential backoff**, and that the differences among the jittered variants are small next to the gap between jitter and none. Full jitter is the simplest to state — the delay is drawn uniformly from `[0, min(cap, base · 2^attempt))`.

### Implementation sketch (Scala)

The token bucket is the load-bearing part: without it, retry volume scales with the error rate, so a policy that behaves well while failures are rare becomes a storm once most calls fail.

```scala
final class TransientFailure(msg: String) extends RuntimeException(msg)

final class RetryBudget(ratePerSecond: Double, capacity: Double):
  private var tokens = capacity
  private var last   = System.nanoTime()

  /** Refill lazily; a retry is admitted only if a whole token is available. */
  def take(): Boolean = synchronized:
    val now = System.nanoTime()
    tokens = math.min(capacity, tokens + (now - last) / 1e9 * ratePerSecond)
    last = now
    if tokens >= 1.0 then { tokens -= 1.0; true } else false

def withRetry[A](
    budget:      RetryBudget,
    maxAttempts: Int  = 5,
    baseMillis:  Long = 100L,
    capMillis:   Long = 10000L,
)(call: => A): A =
  // Bounded: attempt indices run 0 .. maxAttempts - 1, then the failure propagates.
  def attempt(n: Int): A =
    try call
    catch case e: TransientFailure =>
      // The budget is consulted before sleeping, so a depleted budget fails fast.
      if n >= maxAttempts - 1 || !budget.take() then throw e
      val ceiling = math.min(capMillis, baseMillis * (1L << n))
      Thread.sleep(scala.util.Random.nextLong(ceiling))   // full jitter: [0, ceiling)
      attempt(n + 1)
  attempt(0)
```

## Idempotency: the precondition for retrying at all

No retry is safe unless the operation is **idempotent** — executing it twice has the same effect as executing it once. A payment charge, an email send, and an inventory increment are not naturally idempotent, and the dangerous case is a retry after a *timeout* rather than an explicit error: the client cannot distinguish "the request never arrived" from "the request succeeded and the response was lost". Newman and Nygard both treat this as a design-time property rather than a client-side patch. The standard construction is a client-supplied **idempotency key** per logical operation, with the server deduplicating on that key so that a retried request either performs no work or returns the original result. Where an operation cannot be made idempotent, it cannot be safely retried; the remaining option is to surface the failure to a human or to a saga compensating action.

## Bulkheads: bounding which callers a slow dependency can starve

Even with correct timeouts and disciplined retries, a degraded dependency occupies resources for the full duration of every in-flight call. Nygard's bulkhead pattern — named for the watertight compartments that keep a hull breach from sinking a ship — isolates the resource pool (threads, connections, semaphore permits) per dependency, so that exhausting the pool serving a sick `payments` service cannot starve the threads a healthy `catalog` call requires.

Resilience4j provides two bulkhead implementations: a `SemaphoreBulkhead`, which caps concurrent callers with a permit count, and a `ThreadPoolBulkhead`, which gives a dependency its own bounded thread pool and queue.

```java
ThreadPoolBulkheadConfig config = ThreadPoolBulkheadConfig.custom()
    .coreThreadPoolSize(8)
    .maxThreadPoolSize(12)
    .queueCapacity(20)          // bounded — reject rather than queue without limit
    .keepAliveDuration(Duration.ofMillis(500))
    .build();

ThreadPoolBulkhead paymentsBulkhead =
    ThreadPoolBulkheadRegistry.of(config).bulkhead("payments");

Supplier<CompletionStage<Receipt>> isolated =
    ThreadPoolBulkhead.decorateSupplier(paymentsBulkhead, () -> paymentsClient.charge(order));
```

The queue must stay bounded: an unbounded queue does not prevent exhaustion, it relocates it from thread admission to memory and latency. A bulkhead limits the blast radius of a slow dependency; it does not make the calls faster.

## Where the breaker fits

A circuit breaker converts "keep timing out slowly" into "fail immediately for a fixed interval". It observes clean signal only if the calls feeding it already carry bounded timeouts, and it protects the caller's own thread pool only if that pool is bulkheaded off from other dependencies. Timeouts, jittered retries under a budget, idempotency, and bulkheads are the substrate; the breaker is the alarm placed on top of it.

## Pitfalls

- **A single "timeout" setting configures only one of the two clocks.** Many clients default the connect timeout to the read timeout or leave it unset, so an unreachable host holds a caller thread for the full read budget instead of one RTT.
- **Retries at every layer multiply.** Three attempts at each of five hops turn one bottom-level failure into 243 calls; the amplification is invisible in each layer's own configuration.
- **Exponential backoff without jitter re-synchronises the herd.** Clients that failed at the same instant wake at the same instant on every subsequent doubling, so the collision recurs at each attempt.
- **A retry policy without a budget is stable only while errors are rare.** The retry rate is proportional to the error rate, so a policy validated at a 1% error rate emits an order of magnitude more retry traffic at 10%.
- **A timeout without an idempotency key makes duplicate side effects unavoidable.** The client cannot tell a lost request from a lost response, so retrying a charge may charge twice.
- **An unbounded bulkhead queue defers rejection instead of preventing exhaustion.** Requests accumulate in the queue, and by the time a worker dequeues one, the caller's deadline has already passed — work is performed for a response nobody is waiting for.
- **Deadlines that are not propagated reset at every hop.** Each service applies its own timeout to a request the originator abandoned, so downstream capacity is spent on results that will be discarded.
