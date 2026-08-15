---
title: "Scatter/Gather: Buying Latency With Parallelism"
date: 2026-07-26
track: sys-patterns
summary: "Burns' scatter/gather pattern fans a request out to many leaves in parallel and merges their partial answers to cut latency — but the merge step makes the response no faster than the slowest leaf, and fan-out width carries a computational-cost tax."
reading_time: 6
tags: [scatter-gather, latency, fan-out, tail-latency, hedged-requests, distributed-search, burns]
sources:
  - title: "Designing Distributed Systems, 2nd ed. — Ch. 8, Scatter/Gather (Burns, O'Reilly)"
    url: "https://www.oreilly.com/library/view/designing-distributed-systems/9781098156343/ch08.html"
  - title: "The Tail at Scale (Dean & Barroso, Communications of the ACM, 2013)"
    url: "https://cacm.acm.org/research/the-tail-at-scale/"
  - title: "Design Patterns for Container-based Distributed Systems (Burns & Oppenheimer, USENIX HotCloud '16)"
    url: "https://www.usenix.org/conference/hotcloud16/workshop-program/presentation/burns"
  - title: "Scatter–Gather — Distributed Application Architecture Patterns (jurf.github.io)"
    url: "https://jurf.github.io/daap/scalability-patterns/scatter-gather/"
  - title: "Designing Distributed Systems — Scatter-gather & FaaS with event-driven pattern (gemsofcoding.com)"
    url: "https://gemsofcoding.com/Designing-Distributed-Systems-Scatter-Event-Driver/"
---

**Gist.** A single request can require more computation than one node can perform within the caller's latency budget. Scatter/gather splits that computation across a set of **leaves** that run concurrently and merges their partial answers at a **root**, converting a serial cost of N slices into roughly the cost of one slice. The price is that the response cannot complete before the *slowest* leaf replies, so widening the fan-out both raises the probability that some leaf is slow and multiplies the root's fixed per-leaf dispatch and merge overhead.

The [sharded-service pattern](/articles/sys-patterns/2026-07-26-sharded-service-pattern) on this journal addresses a *capacity* problem: state too large for one node, so a root routes each request to the *one* shard that owns the relevant key. Scatter/gather addresses a *serving* problem. Burns places it in the same tree topology — one root, a set of leaves — but inverts the routing rule: the root dispatches the request to *every* leaf rather than to one.

## Root and leaves, dispatched concurrently

The shape is fixed. A **root** accepts the request, **fans it out** concurrently to all leaves (or to every leaf relevant to the query), each leaf performs a bounded slice of work over its own data partition, and the root **merges** the partial results into a single response. The distinction from a replicated service is that a replica there answers a whole request on its own, whereas a leaf here answers one slice of a single request; the copies contribute combined processing capacity rather than redundancy.

The canonical instance is distributed search: an index sharded across a hundred machines, a query that must touch all hundred, and a caller that cannot tolerate a serial traversal. If each leaf spends 20 ms on its slice, the serial cost is roughly 2 seconds and the concurrent cost is approximately the completion time of the slowest leaf. **That last qualifier — slowest, not average — generates the pattern's failure modes.**

## The merge step carries the correctness burden

Fan-out is the mechanically simple half. The **gather/merge** step defines what "the response" means once it is assembled from N independent partial answers computed over N disjoint slices: deduplication, re-ranking by a global score rather than per-leaf scores, combination of partial aggregates (sums, top-k lists, histogram buckets), and — the case most often omitted — the decision of what to emit when a leaf did not answer.

The invariant the merge function must maintain is that **the completeness of the answer is part of the answer**. A merge that assumes all N leaves always respond emits a well-formed response object that is silently wrong the first time a leaf times out: the top-20 list is drawn from 99 shards instead of 100, the sum is short by one partition, and nothing in the response records that fact. Returning the count of leaves that answered alongside the merged payload turns a silent corruption into an observable degradation the caller can act on.

## Failure mode 1: the straggler leaves the mean intact and destroys the tail

Burns notes that latency is bounded by the slowest leaf rather than the average one. Dean and Barroso's "The Tail at Scale" (*Communications of the ACM*, 2013) quantifies why fan-out amplifies this. Their illustrative model: if a single server has a 99th-percentile latency of 1 second and a request must fan out to 100 such servers before it can complete, **63% of requests wait longer than one second** — completing within the budget now requires *all 100* leaves to land inside their own 99th percentile at the same time. At 2,000 servers, at least one slow leaf per request is effectively certain. Their production measurements make the same point without a model: a single leaf's own 99th percentile is **10 ms**, while the 99th percentile of the fanned-out request that waits on every leaf is **140 ms**.

The consequence is counter-intuitive and load-bearing: **widening the fan-out to reduce the mean makes the tail worse**, and the tail is the latency a user observes on a bad day.

The mitigation Dean and Barroso propose, and the standard one for this pattern, is **hedged requests**. Rather than waiting indefinitely on the primary replica, the root waits a short interval — they use approximately the 95th-percentile latency — and then issues a duplicate request to a second replica of the same leaf, accepting whichever response arrives first and cancelling the other. In their benchmark this reduced the 99.9th percentile from **1,800 ms to 74 ms for roughly 2% additional load**. **Tied requests** — enqueueing on two replicas simultaneously and cancelling across servers once one dequeues the work — trade a small resource cost for a larger reduction. The structural point is that the remedy is not making every leaf uniformly fast, which is unattainable at scale, but ensuring that no single leaf gates the response.

## Failure mode 2: the cost curve is not flat in fan-out width

The second problem Burns identifies concerns the shape of the cost curve rather than latency variance. Adding leaves shrinks the *data volume* each leaf must scan, which is the pattern's purpose, but it does not shrink the fixed overhead of dispatching one more request, establishing one more connection, and merging one more partial result at the root. At a fan-out of 10, each leaf performs one tenth of the compute. At a fan-out of 1,000, each leaf is close to idle, the root pays dispatch-and-merge overhead 1,000 times, and there are 1,000 independent opportunities for a straggler or a dead leaf. Widening therefore converts a compute-bound problem into an overhead-and-tail-latency-bound one, and **the operating point is the width at which the falling per-leaf compute cost and the rising per-leaf overhead-plus-straggler cost cross**.

| | Sharded service | Work queue | Scatter/gather |
|---|---|---|---|
| Goal | Capacity (data too big for one node) | Throughput (batch of independent items) | Latency (one request too big to compute serially) |
| Root's job | Route to the *one* correct shard | N/A — workers pull for themselves | Fan out to *all* leaves, then merge |
| Dominant failure | Hot shard | Slow/dead worker (retried later) | Straggler leaf (blocks the response *now*) |
| Scaling knob | Shard count vs. data volume | Worker count vs. queue depth | Leaf count vs. dispatch/merge overhead |

### Implementation sketch (Scala)

The load-bearing elements are a hard per-leaf deadline, a hedge fired after a delay shorter than that deadline, and a merge that accepts absence as a normal input.

```scala
import java.util.concurrent.CompletableFuture
import java.util.concurrent.TimeUnit.MILLISECONDS
import java.time.Duration
import scala.jdk.FutureConverters.*
import scala.concurrent.{Future, ExecutionContext}

case class Hit(id: String, score: Double)
case class Leaf(primary: String, backup: String)
case class Merged(results: Seq[Hit], leavesAnswered: Int, leavesTotal: Int)

val leafDeadline = Duration.ofMillis(150) // one leaf must not gate the response
val hedgeDelay   = Duration.ofMillis(50)  // ~p95 of a healthy leaf

def query(replica: String, q: String): CompletableFuture[Seq[Hit]] = ???

/** Race the primary against a backup issued only if the primary is still
  * outstanding after hedgeDelay; whichever settles `race` first cancels the other. */
def hedged(leaf: Leaf, q: String)(using ExecutionContext): Future[Option[Seq[Hit]]] =
  val primary = query(leaf.primary, q)
  val race    = new CompletableFuture[Seq[Hit]]()
  primary.whenComplete((hits, err) => if err == null then race.complete(hits))
  CompletableFuture.runAsync(
    () =>
      if !primary.isDone then
        val backup = query(leaf.backup, q)
        backup.whenComplete((hits, err) => if err == null then race.complete(hits))
        race.whenComplete((_, _) => backup.cancel(true))
    ,
    // delayedExecutor is the timer: nothing is dispatched until hedgeDelay elapses
    CompletableFuture.delayedExecutor(hedgeDelay.toMillis, MILLISECONDS))
  race.whenComplete((_, _) => primary.cancel(true))
  race.orTimeout(leafDeadline.toMillis, MILLISECONDS)
    .thenApply(Option(_))
    .exceptionally(_ => None)      // a missing leaf is data, not an exception
    .asScala

def scatterGather(leaves: Seq[Leaf], q: String)(using ExecutionContext): Future[Merged] =
  Future.sequence(leaves.map(hedged(_, q))).map: partials =>
    val hits = partials.flatten.flatten.sortBy(-_.score)
    Merged(hits.take(20), partials.count(_.isDefined), leaves.size)
```

The count `leavesAnswered` is not diagnostic decoration: it is the only field distinguishing a complete answer from a degraded one, and callers that ignore it re-introduce the silent-partial-result failure the merge step was written to prevent.

## Pitfalls

- **A merge function without a missing-leaf branch produces a plausible wrong answer.** The symptom is a top-k list or aggregate that is quietly short by one partition; the cause is treating a leaf timeout as an exception that propagates or as an empty result indistinguishable from a genuinely empty shard.
- **Per-leaf timeouts absent, only a global one.** The symptom is the whole fan-out completing at the global deadline whenever any single leaf hangs; the cause is that without a per-leaf budget the slowest leaf, not the deadline, sets the response time for every other leaf's already-completed work.
- **A hedge delay set longer than the per-leaf deadline.** The symptom is the backup replica never being issued; the cause is that the leaf budget expires before the hedge timer fires, so the mitigation is present in the code and inert at runtime.
- **Hedging without cancelling the loser.** The symptom is load growth proportional to fan-out rather than the ~2% Dean and Barroso report; the cause is that both replicas run to completion when the abandoned request is not cancelled.
- **Increasing fan-out width to reduce mean latency.** The symptom is a falling median with a rising 99th percentile; the cause is that each additional leaf adds an independent chance of a straggler while per-leaf compute is already small relative to dispatch and merge overhead.
- **Re-ranking on per-leaf scores.** The symptom is result ordering that changes with shard assignment; the cause is that scores computed against a single partition's statistics are not comparable across partitions without a global normalisation at the root.
