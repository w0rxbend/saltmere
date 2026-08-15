---
title: "The Priority Queue Pattern: Starvation, Aging, and Multi-Level Feedback in a Worker Pool"
date: 2026-08-15
track: sys-patterns
summary: "Letting urgent work jump the line is the easy half of the Priority Queue pattern. The hard half is that a sustained stream of high-priority work can leave low-priority tasks waiting indefinitely. The mitigations — aging, multi-level feedback queues, weighted fair queueing — come from operating-system CPU schedulers and port to a worker pool draining SQS or RabbitMQ."
reading_time: 7
tags: [priority-queue, starvation, aging, scheduling, mlfq, workers]
sources:
  - title: "Priority Queue pattern — Azure Architecture Center"
    url: "https://learn.microsoft.com/en-us/azure/architecture/patterns/priority-queue"
  - title: "Scheduling: The Multi-Level Feedback Queue (OSTEP, Ch. 8) — Arpaci-Dusseau"
    url: "https://pages.cs.wisc.edu/~remzi/OSTEP/cpu-sched-mlfq.pdf"
  - title: "Queue-Based Load Leveling pattern — Azure Architecture Center"
    url: "https://learn.microsoft.com/en-us/azure/architecture/patterns/queue-based-load-leveling"
  - title: "Python standard library — heapq"
    url: "https://docs.python.org/3/library/heapq.html"
---

**Gist.** A plain [work queue](/articles/sys-patterns/2026-07-26-work-queue-pattern) is first-in-first-out (FIFO), so a payment confirmation waits behind a nightly analytics batch that happened to arrive first. The **Priority Queue pattern** attaches a priority to each task so that, in the Azure Architecture Center's wording, "a workload processes high-priority requests more quickly than lower-priority ones." The cost is starvation: under strict priority ordering, low-priority work is served only while the high-priority side is empty, and if it never empties, the low-priority side never advances and **no error is raised**.

## Two shapes, one guarantee

The Azure Architecture Center describes two implementations. The first is a **single priority-ordered queue**: one queue that "orders messages by priority, ensuring that consumers process higher-priority messages before lower-priority ones." This requires a broker that supports priority ordering — RabbitMQ's `x-max-priority` argument, or a database-backed queue whose claim statement carries `ORDER BY priority`.

The second is **multiple queues**, one per priority level. Here the consumer topology, not the broker, decides whether starvation is possible:

| Topology | Mechanism | Cost | Starvation risk |
|----------|--------------|------|-----------------|
| Single ordered queue | Broker sorts by priority | Low | High |
| Multi-queue, single consumer pool | Workers drain the high queue first; touch low only when high is empty | Low | High |
| Multi-queue, multiple pools | Dedicated pools per queue, sized per priority | Higher | Low — the low-priority queue always retains some workers |

The single-pool model is the trap. "Single consumer pools always process higher-priority messages before lower-priority ones" — which is strict priority scheduling, and the condition under which the bottom of the queue is starved.

## The failure mode: starvation

The invariant of strict priority is that **a worker dequeues from level *k* only if every level above *k* is observed empty at that instant**. The invariant is locally correct on every dequeue and globally fatal: it never bounds waiting time at the lower levels. Azure states the consequence directly — this setup "can lead to lower-priority messages being continually delayed and potentially never processed."

The observable symptom is distinctive. Throughput is healthy, worker utilisation is high, the error rate is zero, and one queue's depth sits at a non-zero constant while its **oldest-message age grows without bound**. Depth alone does not distinguish a starved queue from a busy one; age does.

The mitigations come from CPU scheduling, and they all work by making effective priority a function of waiting time rather than a constant.

**Aging (priority boosting).** Raise a task's effective priority the longer it has waited, so a delayed item eventually outranks fresh arrivals. Azure's recommendation to "dynamically increase the priority of old messages to ensure that low-priority messages eventually get processed" is aging under another name. A worker pulling from an in-process heap can implement it directly. Python's `heapq` is a min-heap, so *lower number means more urgent*, and the boost is subtracted in proportion to waiting time:

```python
import heapq, itertools, time

class AgingPriorityQueue:
    """Min-heap: lower number = more urgent. Long waits get boosted so
    nothing starves."""
    def __init__(self, boost_per_sec: float = 0.5):
        self._heap: list[list] = []
        self._seq = itertools.count()      # tiebreak, keeps pops FIFO-stable
        self._boost = boost_per_sec

    def push(self, item, base_priority: int) -> None:
        now = time.monotonic()
        # entry = [effective, seq, enqueued_at, base, item]
        heapq.heappush(
            self._heap,
            [base_priority, next(self._seq), now, base_priority, item],
        )

    def pop(self):
        now = time.monotonic()
        for e in self._heap:                       # re-age everyone
            waited = now - e[2]
            e[0] = e[3] - self._boost * waited     # older -> smaller -> more urgent
        heapq.heapify(self._heap)                  # O(n) restore of heap order
        _eff, _seq, _enq, _base, item = heapq.heappop(self._heap)
        return item
```

A base-priority-5 task that has waited 12 seconds has effective priority `5 - 0.5*12 = -1` and therefore outranks a freshly arrived priority-0 task. The cost is that re-aging every element on each `pop` is **O(n) per dequeue**, plus the O(n) `heapify`; this is acceptable for a modest in-memory queue and not for a large one, where the alternatives are to re-age on a timer or to promote aged items a level at a time.

**Multi-level feedback queues (MLFQ).** The operating-system scheduler's answer. OSTEP (Ch. 8) states the rules: "When a job enters the system, it is placed at the highest priority (the topmost queue)"; "Once a job uses up its time allotment at a given level (regardless of how many times it has given up the CPU), its priority is reduced (i.e., it moves down one queue)"; and, as the anti-starvation rule, "After some time period S, move all the jobs in the system to the topmost queue." **That last rule is aging applied to whole queues** — a periodic bulk boost rather than a continuous per-item recomputation, which removes the O(n)-per-dequeue cost and pays it once per period S instead.

OSTEP also names the abuse case: **gaming**, where a job performs a small amount of I/O to relinquish the processor before its allotment expires and so retains its high priority. The documented fix is to "perform better accounting of CPU time at each level of the MLFQ" — track cumulative allotment consumed at a level regardless of how many times the job yields voluntarily. The analogue in a worker pool is a producer that labels all of its traffic high-priority and floods the top queue; the boost period S bounds how long the other levels wait before being returned to the top queue, and per-tenant accounting is what prevents one producer from holding the top level indefinitely.

**Weighted fair queueing (WFQ).** Rather than strict "high before low", allocate each priority level a *share* of the pool — for example 8:3:1 across three queues. The high level receives most of the capacity while the low level retains at least one consumer, so it drains slowly rather than not at all. This is the multi-pool row of the table, with pool sizes set by weight.

### Implementation sketch (Scala)

Deficit-based weighted selection over per-priority queues. The load-bearing idea is that **the choice of queue is made by a credit counter, not by emptiness of the level above**, so every weighted level makes progress whenever it has work.

```scala
final case class Level[A](weight: Int, q: java.util.Queue[A])

final class WeightedDrain[A](levels: Vector[Level[A]]):
  // One credit counter per level; refilled by weight each round.
  private val credit = Array.fill(levels.size)(0)

  /** Returns the next task, or None when every level is empty. */
  def next(): Option[A] =
    var attempts = 0
    while attempts <= levels.size do
      val i = pick()
      if i >= 0 then
        credit(i) -= 1
        val t = levels(i).q.poll()
        if t != null then return Some(t)
      else
        // No level holds credit: start a new round.
        levels.indices.foreach(j => credit(j) += levels(j).weight)
        attempts += 1
    None

  /** Highest-weight level that still has both credit and work. */
  private def pick(): Int =
    levels.indices
      .filter(i => credit(i) > 0 && !levels(i).q.isEmpty)
      .maxByOption(levels(_).weight)
      .getOrElse(-1)
```

With weights `8:3:1`, a round in which every level has work yields twelve dequeues, one of them from the lowest level, however much work the top level holds. Replacing `pick` with "first non-empty level" recovers strict priority — and its starvation.

## Choosing

Strict priority is the simplest correct choice **only when low-priority work is genuinely droppable** — best-effort telemetry, backfill with no deadline. Once a low-priority task carries any deadline, the choice is aging or a weighted split, and the operational requirement is an alert on **oldest-message age per priority level**, because that is the signal starvation produces.

Priority ordering does not address total overload: when the arrival rate exceeds service capacity across all levels, the top queue backs up as well, and reordering cannot create capacity. The pattern therefore pairs with **Queue-Based Load Leveling** to absorb bursts, and with Azure's recommendation to "scale the size of consumer pools based on the length of the queue they're servicing."

## Pitfalls

- **Monitoring queue depth instead of message age.** A starved queue holds a constant depth, which looks like a steady backlog rather than a fault; only oldest-message age grows without bound and identifies it.
- **Re-aging the whole heap on every dequeue.** The `AgingPriorityQueue` above is O(n) per `pop`; at large n the aging pass dominates the work being scheduled. A periodic bulk boost, as in the MLFQ rule for period S, moves that cost off the dequeue path.
- **Treating a single ordered queue as sufficient.** Broker-side priority ordering changes which message is delivered first; it does not bound how long the lowest priority waits, so starvation survives the migration to a priority-capable broker.
- **Trusting producer-supplied priority.** A producer that marks all of its traffic high-priority collapses the scheme to FIFO with extra steps — the worker-pool form of the gaming behaviour OSTEP describes, and it requires per-tenant accounting rather than a larger top-level pool.
- **Assuming a dedicated low-priority pool cannot starve.** It guarantees consumers, not throughput; if the low pool's tasks are slower than their arrival rate, the queue grows regardless of the reserved share.
- **Aging that is unbounded and untuned.** A boost large enough to make every aged task outrank fresh arrivals inverts the ordering the pattern exists to provide, and the payment confirmation then waits behind the analytics batch it was meant to overtake.
