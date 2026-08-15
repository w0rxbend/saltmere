---
title: "Circuit breakers: containing a slow dependency"
date: 2026-07-24
track: microservices
summary: "A retry storm against a struggling service turns one slow dependency into a fleet-wide outage. A circuit breaker trips after enough failures, fails fast for a cooldown window, then probes for recovery — converting a cascading failure into a contained one."
reading_time: 6
tags: [circuit-breaker, resiliency, resilience4j, timeouts, newman, jvm]
sources:
  - title: "Sam Newman, Building Microservices (2nd ed.) — Resiliency (Ch. 12)"
    url: "https://www.oreilly.com/library/view/building-microservices-2nd/9781492034018/"
  - title: "Resilience4j — CircuitBreaker docs"
    url: "https://resilience4j.readme.io/docs/circuitbreaker"
  - title: "Resilience4j — releases"
    url: "https://github.com/resilience4j/resilience4j/releases"
  - title: "Martin Fowler — CircuitBreaker"
    url: "https://martinfowler.com/bliki/CircuitBreaker.html"
---

**Gist.** The dominant failure mode of a remote dependency is not a crash but latency: a service that answers in 5 seconds instead of 50 ms holds caller threads occupied, and callers that retry occupy more, so the slowness propagates upstream until the calling system is saturated. A **circuit breaker** interposes a state machine between caller and dependency that counts recent failures and, above a threshold, rejects calls immediately rather than blocking on them. The cost is that the breaker rejects calls that would have succeeded — it trades availability of individual requests for survival of the caller's thread pool, and it requires the caller to define what a rejected call returns.

## The propagation mechanism

Newman's resiliency chapter (Ch. 12 of *Building Microservices*, 2nd ed.) frames the problem in terms of finite caller resources. A synchronous call occupies a thread from the moment of dispatch to the moment of response. Under normal latency the occupancy is short enough that a bounded pool of *n* threads sustains a request rate of roughly *n* / latency. When the dependency's latency rises by two orders of magnitude, sustainable throughput falls by the same factor, and **arriving requests queue behind occupied threads rather than being served**. The caller then exhibits the same symptom to *its* callers, and the saturation walks up the call graph. Retries accelerate it: each retry adds a fresh occupancy against a dependency that is already the bottleneck.

The observation that matters is that **the caller, not the dependency, is where the damage compounds**. The dependency is merely slow; the caller is exhausted. A circuit breaker is therefore a caller-side control, deployed per dependency, and it works by refusing to spend caller resources on a dependency that recent evidence says will not answer.

## The state machine

Fowler's description and the Resilience4j implementation share three principal states.

- **CLOSED.** Calls pass through. Every outcome — success, exception, or slow completion — is recorded in a sliding window. When the window holds at least the configured minimum number of calls and the failure rate crosses the threshold, the breaker transitions to OPEN.
- **OPEN.** Calls are not attempted. Resilience4j rejects them with `CallNotPermittedException`, which returns in the time it takes to read the breaker's state rather than the time it takes to time out a socket. The state persists for the configured wait duration.
- **HALF_OPEN.** A bounded number of probe calls is permitted. Their outcomes populate a fresh window; if the failure rate over those probes is below the threshold the breaker returns to CLOSED, otherwise it returns to OPEN.

Two details of the Resilience4j transition rules are load-bearing. First, **the transition from OPEN to HALF_OPEN is triggered by an incoming call after the wait duration has elapsed, not by a background timer**, unless `automaticTransitionFromOpenToHalfOpenEnabled` is set; a breaker on an idle dependency therefore stays nominally OPEN until traffic returns. Second, **calls beyond the permitted probe count in HALF_OPEN are rejected**, so the half-open phase does not itself become a load spike against a service that has not yet recovered. Resilience4j additionally defines the special states DISABLED, FORCED_OPEN and METRICS_ONLY, which bypass the transition logic and exist for operational override.

The window is either **count-based** — the last *N* recorded outcomes — or **time-based**, aggregating the outcomes of the last *N* seconds. The count-based window makes the trip condition independent of traffic rate; the time-based window makes it independent of call volume. Neither is correct in general. Independently of the window type, `minimumNumberOfCalls` sets how many outcomes must be recorded before Resilience4j calculates a failure rate at all; below that count the breaker does not open, so a window holding three outcomes cannot trip on a single failure. Its default is 100, which is larger than a deliberately small window and must be lowered alongside it.

## Slow calls are not failures

A breaker that counts only exceptions does not fire on the failure mode described above, because **a call that hangs has neither succeeded nor failed — it is still outstanding, and an outstanding call contributes nothing to the window**. Resilience4j closes this gap in two complementary ways.

The first is `slowCallDurationThreshold` together with `slowCallRateThreshold`: a call that *completes* but takes longer than the duration threshold is recorded as a slow call, and the breaker opens when the slow-call rate crosses its threshold, independently of the failure rate. This handles degradation but still requires the call to finish.

The second is a **time limiter**, a separate Resilience4j decorator that imposes a hard ceiling per attempt and produces a `TimeoutException` when the ceiling is exceeded. Composed with the breaker, it converts an unbounded wait into a bounded one and into a countable failure. **Without a bounded timeout the breaker's window can remain empty while every thread is blocked** — the pathological case in which a breaker is configured, never trips, and contributes nothing.

```java
var cbConfig = CircuitBreakerConfig.custom()
    .slidingWindowType(COUNT_BASED)
    .slidingWindowSize(20)               // the last 20 recorded outcomes
    .minimumNumberOfCalls(20)            // default is 100, larger than the window
    .failureRateThreshold(50)            // open at or above 50% failures
    .slowCallDurationThreshold(Duration.ofMillis(800))
    .slowCallRateThreshold(50)           // open at or above 50% slow calls
    .waitDurationInOpenState(Duration.ofSeconds(10))
    .permittedNumberOfCallsInHalfOpenState(3)
    .build();

var registry = CircuitBreakerRegistry.of(cbConfig);
var cb  = registry.circuitBreaker("payments");
var tl  = TimeLimiter.of(Duration.ofSeconds(1));       // hard ceiling per call

Supplier<CompletionStage<Receipt>> guarded =
    CircuitBreaker.decorateCompletionStage(cb,
        TimeLimiter.decorateCompletionStage(tl, scheduler, () -> paymentsClient.charge(order)));

CompletableFuture.supplyAsync(() -> guarded.get())
    .exceptionally(ex -> Receipt.deferred(order));      // fallback
```

With this configuration, degradation of `payments` is capped at 1 s per attempt; those timeouts are recorded as failures; and once **ten of the twenty outcomes in the window are failures** the rate reaches 50 % and the breaker opens — Resilience4j compares the measured rate against the threshold with *equal or greater than*, so exactly 50 % trips it. Subsequent calls return the `deferred` fallback without occupying a thread for a second each. The ordering of the decorators matters: Resilience4j documents the composition order as retry outside circuit breaker outside rate limiter outside time limiter, so the breaker wraps the time-limited supplier and observes a timeout as a failed call. Inverting the two hides the timeout from the breaker.

### Implementation sketch (Scala)

The transition logic, reduced to the count-based case, with the window as an immutable queue of booleans:

```scala
enum State:
  case Closed
  case Open(since: Long)
  case HalfOpen(probesLeft: Int)

final case class Breaker(
    state: State,
    window: Vector[Boolean],          // true = failure or slow call
    size: Int, minCalls: Int, rate: Double,
    waitMillis: Long, probes: Int):

  private def failureRate: Double =
    if window.isEmpty then 0.0 else window.count(identity).toDouble / window.length

  // the threshold comparison is >=, so a window at exactly the rate opens the breaker
  private def tripped: Boolean = window.length >= minCalls && failureRate >= rate

  /** Returns None when the call is not permitted. */
  def acquire(now: Long): Option[Breaker] = state match
    case State.Closed          => Some(this)
    case State.Open(since) if now - since >= waitMillis =>
      Some(copy(state = State.HalfOpen(probes - 1), window = Vector.empty))
    case State.Open(_)         => None
    case State.HalfOpen(0)     => None
    case State.HalfOpen(n)     => Some(copy(state = State.HalfOpen(n - 1)))

  def record(failed: Boolean, now: Long): Breaker =
    val next = copy(window = (window :+ failed).takeRight(size))
    next.state match
      case State.Closed if next.tripped => next.copy(state = State.Open(now))
      // half-open decides on the probe outcomes alone: minCalls does not apply
      case State.HalfOpen(0) if next.window.length >= probes =>
        if next.failureRate >= rate then next.copy(state = State.Open(now))
        else next.copy(state = State.Closed, window = Vector.empty)
      case _ => next
```

The sketch omits the concurrency control that a real breaker needs: `acquire` and `record` are invoked from many threads, and Resilience4j maintains the window and state under atomic updates rather than by copying a vector.

## Pitfalls

- **A breaker with no timeout never opens.** Hung calls stay outstanding, never enter the sliding window, and the failure rate remains zero while every thread is blocked.
- **A breaker with no fallback converts a slow error into a fast error.** Callers still receive an exception; the only thing gained is thread-pool survival, unless a degraded response — a cached value, a queued job, an explicit 503 — is defined.
- **One breaker shared across unrelated dependencies loses attribution.** A failure of `payments` opens the breaker for `catalog` as well, and the operator cannot tell from the breaker's state which dependency is sick.
- **A count-based window smaller than `minimumNumberOfCalls` never opens the breaker.** The window discards its oldest outcome at `slidingWindowSize`, so the recorded count never reaches the minimum and no failure rate is calculated; shrinking the window without shrinking the default minimum of 100 produces a breaker that is configured and inert.
- **A minimum call count of one opens the breaker on the first failure.** A single transient error then costs a full wait duration of rejected traffic.
- **A wait duration shorter than the dependency's recovery time causes flapping.** Probes arrive while the dependency is still saturated, reopen the breaker, and the probe traffic itself contributes load during every half-open phase.
- **An idle dependency keeps its breaker OPEN indefinitely.** Without `automaticTransitionFromOpenToHalfOpenEnabled`, the OPEN-to-HALF_OPEN transition is evaluated on the next incoming call, so a breaker on a low-traffic path can appear stuck long after the wait duration elapsed.
- **A breaker in front of a saturated shared thread pool does not isolate anything.** Rejecting `payments` calls quickly does not return the threads that `payments` already holds; a bulkhead — a bounded pool per dependency — is what prevents one dependency from starving another. Timeouts, breakers and bulkheads are the three controls Newman treats together.
