---
title: "Request Hedging: cutting tail latency in replicated services"
date: 2026-07-30
track: sys-patterns
summary: "In a replicated service, your p99 isn't set by the average replica — it's set by whichever one happens to be slow right now. Hedged requests send a backup to a second replica after a short delay and take the first answer. Here's the math on why it works and a Go implementation with the cancellation that makes it safe."
reading_time: 5
tags: [tail-latency, hedged-requests, replication, serving-pattern, p99, resiliency]
sources:
  - title: "The Tail at Scale (CACM, Feb 2013) — Jeffrey Dean & Luiz André Barroso"
    url: "https://cacm.acm.org/research/the-tail-at-scale/"
  - title: "Designing Distributed Systems, 2nd ed. (replicated serving patterns) — Brendan Burns"
    url: "https://www.oreilly.com/library/view/designing-distributed-systems/9781098156343/"
  - title: "gRPC — client-side hedging policy (retry & hedging)"
    url: "https://grpc.io/docs/guides/retry/"
  - title: "Good enough is good enough: request hedging — Marc Brooker"
    url: "https://brooker.co.za/blog/2022/08/09/hedging.html"
---

You've replicated your read service across five identical nodes for throughput and availability. Load is balanced, every node is healthy, and yet your p99 latency is three times your median. Why? Because a request's latency is the latency of the *one replica it landed on*, and at any instant some replica is briefly slow — a GC pause, a compaction, a noisy neighbor, a cold cache. Across enough requests you keep drawing the slow one. Dean and Barroso's *The Tail at Scale* named this the central problem of large fan-out systems: **the tail is not an edge case, it's the common case**, because a request that touches many replicas is only as fast as the slowest one it waited on.

## Why one slow replica dominates

The unintuitive part: transient slowness that's rare *per replica* becomes near-certain *per request* once you fan out. Suppose each replica is slow (say >100 ms) just 1% of the time. Hit one replica and you're slow 1% of the time — a nice p99. But if a single user request must gather results from **100** replicas (a sharded search, a scatter-gather — see the scatter-gather pattern article here) and waits for all of them, the chance that *at least one* is slow is `1 − 0.99^100 ≈ 63%`. Your per-replica p99 became a per-request *median*. Rare local hiccups compound into a fat overall tail.

## Hedged requests

The fix Dean and Barroso propose is disarmingly simple. Send the request to one replica. If it hasn't answered within a short delay — say the **p95** of normal latency — send a **second, identical** request to a *different* replica. Take whichever response comes back first and cancel the other. That's a **hedged request**.

It works because slowness is usually *transient and uncorrelated*: the replica that's mid-GC-pause right now is almost certainly not the same one the backup lands on, so the backup routes around the local hiccup. And because you only hedge after the p95 delay, you send a backup for **at most ~5% of requests** — a tiny amount of extra load in exchange for chopping the tail. Dean and Barroso report that in a real Google service, hedging after a 10 ms delay cut the 99.9th-percentile latency roughly in half while adding only single-digit-percent extra requests.

The delay is the whole design. Hedge too eagerly (delay near zero) and you double your traffic — every request runs twice. Hedge too late and slow requests still wait most of their slow time before help arrives. Setting the delay at the p95 is the sweet spot: normal requests finish before the timer fires and never hedge at all; only the genuinely-slow tail pays for a backup.

## In Go, with the cancellation that matters

The critical part isn't sending the second request — it's **cancelling the loser** so you don't pay double downstream cost. `context` makes this clean:

```go
func hedgedGet(ctx context.Context, replicas []Client, hedgeAfter time.Duration) (Result, error) {
    ctx, cancel := context.WithCancel(ctx)
    defer cancel()                       // cancels the loser the instant we return

    results := make(chan Result, len(replicas))
    errs := make(chan error, len(replicas))

    launch := func(c Client) {
        r, err := c.Get(ctx)             // ctx cancellation propagates to the replica
        if err != nil { errs <- err; return }
        results <- r
    }

    go launch(replicas[0])               // primary request

    timer := time.NewTimer(hedgeAfter)   // hedgeAfter ~= p95 of normal latency
    defer timer.Stop()
    hedged := 0

    for {
        select {
        case r := <-results:
            return r, nil                // first answer wins; defer cancel() kills the rest
        case <-timer.C:
            hedged++
            if hedged < len(replicas) {
                go launch(replicas[hedged])   // fire a backup at another replica
                timer.Reset(hedgeAfter)       // and be willing to hedge again
            }
        case <-ctx.Done():
            return Result{}, ctx.Err()
        }
    }
}
```

Two properties make this safe. The winning branch returns immediately and the deferred `cancel()` propagates through the shared `ctx` to every outstanding replica call, so the losers stop working rather than finishing wasted work. And requests must be **idempotent** or read-only — you're deliberately sending the same operation to two servers, so a hedged *write* without idempotency keys (see that article here) can double-apply. Hedge reads freely; hedge writes only when they're idempotent.

gRPC bakes this in as a **hedging policy** in its service config (`maxAttempts`, `hedgingDelay`), so for gRPC services you often get it declaratively without writing the loop above. Dean and Barroso also describe a stronger variant, **tied requests**, where the two replicas are told about each other and the first to *start* executing tells the other to drop it — trimming even the tiny window of duplicated work. Hedging is the 90%-of-the-benefit version you can ship today.

## When not to

Hedging spends capacity to buy latency, so it's for services with **headroom** and **transient, uncorrelated** slowness. If your tail is caused by a *correlated* problem — every replica overloaded, a shared-dependency brownout — hedging pours fuel on the fire by adding load exactly when you're least able to serve it. Cap the hedge rate, and consider disabling it under high utilization. It's a scalpel for jitter, not a fix for saturation.

**Try next:** Wrap a service that sleeps a random 5–150 ms (with a 2% chance of a 500 ms stall) behind `hedgedGet` across three replicas. Measure p50/p99 with `hedgeAfter` set to `0`, to the p95 (~120 ms), and to `1s`, and plot the three. You'll see the p95 setting collapse the p99 while the extra request count stays near 5% — the entire tail-at-scale argument, reproduced on your laptop.
