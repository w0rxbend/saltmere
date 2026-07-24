---
title: "Circuit breakers: stop a slow dependency from taking you down with it"
date: 2026-07-24
track: microservices
summary: "A retry storm against a struggling service turns one slow dependency into a fleet-wide outage. A circuit breaker trips after enough failures, fails fast for a cooldown, then probes for recovery — turning a cascading failure into a contained one."
reading_time: 5
tags: [circuit-breaker, resiliency, resilience4j, timeouts, newman, jvm]
sources:
  - title: "Sam Newman, Building Microservices (2nd ed.) — Resiliency (Ch. 12)"
    url: "https://www.oreilly.com/library/view/building-microservices-2nd/9781492034018/"
  - title: "Resilience4j — CircuitBreaker docs"
    url: "https://resilience4j.readme.io/docs/circuitbreaker"
  - title: "Resilience4j releases (latest 2.4.0, Mar 2024)"
    url: "https://github.com/resilience4j/resilience4j/releases"
  - title: "Martin Fowler — CircuitBreaker"
    url: "https://martinfowler.com/bliki/CircuitBreaker.html"
---

Newman's resiliency chapter makes an uncomfortable point: the most common way a microservice fails is not a crash — it's getting *slow*. A dependency that answers in 5 seconds instead of 50 ms holds your threads hostage. Callers pile up, retry, and pile up more; the slowness propagates upstream until the whole system is wedged. The single most valuable stability pattern for stopping this is the **circuit breaker**.

The idea borrows from electrics. Wrap every remote call in a breaker that watches the failure rate. While calls succeed it's **closed** (traffic flows). Once failures cross a threshold it **opens**: for a cooldown window it rejects calls *instantly* instead of waiting on a timeout, giving the sick service room to breathe. After the window it goes **half-open**, lets a few probe calls through, and either closes (recovered) or opens again (still sick).

## Wiring one up with Resilience4j

Resilience4j (2.4.0 as of March 2024) is the standard JVM library for this, and it composes cleanly with Scala or plain Java. The key insight most people miss: a circuit breaker is *necessary but not sufficient*. It counts failures, but a hung call never fails — it just hangs. So you pair it with a **time limiter** that turns "too slow" into a countable failure.

```java
var cbConfig = CircuitBreakerConfig.custom()
    .slidingWindowType(COUNT_BASED)
    .slidingWindowSize(20)               // look at the last 20 calls
    .failureRateThreshold(50)            // open if >50% failed
    .slowCallDurationThreshold(Duration.ofMillis(800))
    .slowCallRateThreshold(50)           // a slow call counts as a failure
    .waitDurationInOpenState(Duration.ofSeconds(10))   // cooldown
    .permittedNumberOfCallsInHalfOpenState(3)
    .build();

var registry = CircuitBreakerRegistry.of(cbConfig);
var cb  = registry.circuitBreaker("payments");
var tl  = TimeLimiter.of(Duration.ofSeconds(1));       // hard ceiling per call

Supplier<CompletionStage<Receipt>> guarded =
    TimeLimiter.decorateCompletionStage(tl, scheduler,
        CircuitBreaker.decorateCompletionStage(cb, () -> paymentsClient.charge(order)));

CompletableFuture.supplyAsync(() -> guarded.get())
    .exceptionally(ex -> Receipt.deferred(order));      // your fallback
```

When `payments` degrades, the time limiter caps each attempt at 1 s, those slow attempts count toward the breaker, and after eleven bad calls in the window the breaker opens — subsequent calls fail in *microseconds* into your `deferred` fallback instead of stacking up 1-second waits. That difference is what keeps your thread pool alive.

## The parts people get wrong

A breaker without a **fallback** just converts a slow error into a fast error — decide what "degraded but useful" means (a cached value, a queued job, a friendly 503). A breaker without a **bounded timeout** never trips on the failure mode that matters most. And a breaker shared across *unrelated* dependencies hides which one is sick — give each downstream its own named breaker and export its state as a metric, so "payments breaker is OPEN" shows up on a dashboard before a human notices.

**Try next:** put the config above in front of a toy service, then add an artificial `Thread.sleep(2000)` to the downstream and watch the breaker flip to OPEN in your logs. Then add a **bulkhead** (a separate, bounded thread pool per dependency) so a saturated `payments` can't starve the threads your `catalog` calls need — timeouts, breakers, and bulkheads are the three legs Newman leans on together.
