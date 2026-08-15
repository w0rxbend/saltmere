---
title: "Request Hedging: cutting tail latency in replicated services"
date: 2026-07-30
track: sys-patterns
summary: "In a replicated service the 99th-percentile latency is set not by the average replica but by whichever replica is transiently slow. Hedged requests issue a backup to a second replica after a short delay and take the first answer. This article derives why that works and shows the cancellation that makes it safe, in Go and in Scala."
reading_time: 6
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

**Gist.** In a replicated read service every replica is occasionally slow — a garbage-collection (GC) pause, a storage compaction, a noisy co-tenant, a cold cache — so the latency a client observes is the latency of the single replica its request happened to land on. A **hedged request** sends the same request to a second replica after a short delay and returns whichever response arrives first, converting a per-replica tail into a near-minimum over two draws. The cost is duplicated work: extra requests, extra downstream capacity consumed, and correctness obligations on the operation being duplicated.

## Why one slow replica dominates

Transient slowness that is rare *per replica* becomes likely *per request* once a request fans out. Dean and Barroso's *The Tail at Scale* states the arithmetic: if a replica is slow one per cent of the time, a request served by a single replica is slow one per cent of the time, but a request that must gather results from **100 replicas and wait for all of them** is slow whenever at least one of them is — assuming independence, with probability `1 − 0.99^100 ≈ 63%`. **The rate that was a per-replica 99th percentile dominates the per-request distribution outright.** The fan-out is the amplifier; the per-replica hiccup rate need not change at all.

Two properties of the underlying slowness are load-bearing for everything that follows. It is **transient** — the replica recovers on a timescale shorter than the client's patience — and it is **uncorrelated** across replicas, meaning the event that makes replica A slow does not simultaneously make replica B slow. Both assumptions are empirical, and both can fail (see Pitfalls).

## The hedging mechanism

The procedure is a two-state timer. Issue the request to one replica and start a timer of length `hedgeAfter`. If the response arrives before the timer fires, return it and stop. If the timer fires first, issue a **second, identical** request to a *different* replica; return the first response from either, and cancel the outstanding one.

The observed latency is therefore the minimum of the primary's latency and (`hedgeAfter` + the backup's latency). Under the uncorrelated-slowness assumption the backup is drawn from the normal latency distribution rather than the stalled one, so a stall on the primary no longer sets the request's latency.

**The delay is the entire design.** Setting `hedgeAfter` to the **95th percentile of normal latency** bounds the fraction of requests that hedge at approximately **five per cent**, because by construction 95 per cent of requests complete before the timer fires. A delay near zero duplicates every request and doubles offered load. A delay far above the 95th percentile leaves slow requests waiting out most of their stall before help is dispatched. Dean and Barroso report a BigTable benchmark in which sending the hedge after a **10 ms** delay cut the **99.9th-percentile** latency of a 1,000-key lookup by more than an order of magnitude while issuing about **two per cent** more requests.

## Cancellation and idempotence

Sending the backup is the easy half. **Cancelling the loser is what keeps the added load bounded**: without cancellation the duplicated request runs to completion, consuming downstream capacity long after its result has been discarded, and the "five per cent extra requests" figure becomes five per cent extra *completed* work at the slowest replicas — exactly the ones least able to absorb it.

The second obligation is on the operation itself. Hedging deliberately submits the same operation twice, so it is safe only for **read-only or idempotent** operations. A hedged write without an idempotency key can be applied twice, since the "cancelled" request may already have committed at its replica before the cancellation is observed.

```go
func hedgedGet(ctx context.Context, replicas []Client, hedgeAfter time.Duration) (Result, error) {
    ctx, cancel := context.WithCancel(ctx)
    defer cancel()                       // cancels the loser the instant the function returns

    results := make(chan Result, len(replicas))

    launch := func(c Client) {
        r, err := c.Get(ctx)             // ctx cancellation propagates to the replica
        if err != nil { return }         // ... a failed replica is left to the other attempts
        results <- r
    }

    go launch(replicas[0])               // primary request

    timer := time.NewTimer(hedgeAfter)   // hedgeAfter ~= p95 of normal latency
    defer timer.Stop()
    hedged := 0

    for {
        select {
        case r := <-results:
            return r, nil                // first answer wins; deferred cancel() stops the rest
        case <-timer.C:
            hedged++
            if hedged < len(replicas) {
                go launch(replicas[hedged])   // fire a backup at another replica
                timer.Reset(hedgeAfter)       // and remain willing to hedge again
            }
        case <-ctx.Done():
            return Result{}, ctx.Err()
        }
    }
}
```

The winning branch returns immediately and the deferred `cancel()` propagates through the shared context to every outstanding replica call.

### Implementation sketch (Scala)

The same state machine expressed with `Promise` as the first-writer-wins arbiter. `Promise.tryComplete` is the single point at which the race is decided: it returns `true` for exactly one caller, so the completion is unambiguous and the losing branches become no-ops.

```scala
import scala.concurrent.{ExecutionContext, Future, Promise}
import scala.concurrent.duration.FiniteDuration
import java.util.concurrent.atomic.AtomicBoolean

trait Replica[A]:
  /** `cancelled` is polled by the call so a decided race stops work in flight. */
  def get(cancelled: AtomicBoolean): Future[A]

def hedged[A](replicas: Vector[Replica[A]], hedgeAfter: FiniteDuration)
             (using ec: ExecutionContext, sched: java.util.concurrent.ScheduledExecutorService): Future[A] =
  val winner    = Promise[A]()
  val cancelled = AtomicBoolean(false)

  def launch(i: Int): Unit =
    replicas(i).get(cancelled).onComplete: outcome =>
      if winner.tryComplete(outcome) then cancelled.set(true)  // first outcome decides

  launch(0)

  // one backup per elapsed hedgeAfter, until replicas are exhausted or the race is decided
  val backups = replicas.indices.drop(1)
  backups.foreach: i =>
    sched.schedule(
      (() => if !winner.isCompleted then launch(i)): Runnable,
      hedgeAfter.toMillis * i, java.util.concurrent.TimeUnit.MILLISECONDS)

  winner.future
```

The invariant is that `winner` is completed exactly once and `cancelled` is set only after it is. Note what `tryComplete` treats as a decision: the *first outcome*, success or failure, so a replica that fails quickly ends the race and cancels the slower attempt. Making failures non-deciding — the behaviour gRPC's hedging policy expresses through its non-fatal status codes — requires completing `winner` only on success and falling back to the last failure once every attempt is exhausted.

## Declarative and stronger variants

gRPC exposes hedging as a **hedging policy** in its service configuration, parameterised by `maxAttempts` and `hedgingDelay`, so a gRPC client obtains the behaviour above without an explicit loop. Dean and Barroso also describe **tied requests**: the two replicas are informed of each other, and whichever begins executing first instructs the other to drop the request, shrinking the window of duplicated work relative to delay-based hedging.

## When hedging is inapplicable

Hedging spends capacity to buy latency. It applies to services with spare **headroom** and **transient, uncorrelated** slowness. Where the tail arises from a *correlated* cause — all replicas overloaded, a shared dependency degraded — hedging adds load precisely when the system is least able to serve it. Capping the hedge rate and disabling hedging above a utilisation threshold bounds that feedback.

## Pitfalls

- **Hedging under saturation deepens the outage.** Symptom: the tail worsens after hedging is enabled. Cause: the slowness is correlated across replicas, so the backup lands on an equally overloaded replica and the duplicate requests raise utilisation further.
- **Hedging without cancellation multiplies downstream work.** Symptom: request counts at the storage layer rise more than the client-side hedge rate predicts. Cause: the losing request is ignored rather than cancelled and still runs to completion.
- **A hedge delay near zero duplicates every request.** Symptom: offered load approximately doubles. Cause: the timer fires before typical responses arrive, so the backup is sent on the normal path rather than the tail.
- **A hedge delay far above the 95th percentile buys little.** Symptom: the 99.9th percentile is unchanged. Cause: the slow request has already waited out most of its stall by the time the backup is dispatched.
- **Hedging a non-idempotent write can double-apply it.** Symptom: duplicate side effects with no error reported. Cause: the cancelled request may have committed at its replica before the cancellation was observed.
- **Routing the backup to the same replica removes the benefit.** Symptom: hedged requests are as slow as unhedged ones. Cause: the backup is drawn from the same stalled process, so the minimum is taken over two correlated samples.
- **A static hedge delay drifts out of calibration.** Symptom: the hedge rate diverges from five per cent. Cause: the delay was fixed at a past 95th percentile while the latency distribution moved.
