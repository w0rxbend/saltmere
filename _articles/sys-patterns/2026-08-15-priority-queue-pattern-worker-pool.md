---
title: "The Priority Queue Pattern: Starvation, Aging, and Multi-Level Feedback in a Worker Pool"
date: 2026-08-15
track: sys-patterns
summary: "Letting urgent work jump the line is the easy half of the Priority Queue pattern. The hard half is that a firehose of high-priority work can leave low-priority tasks waiting forever. The fixes — aging, multi-level feedback queues, weighted fair queueing — were solved decades ago by OS CPU schedulers, and they port cleanly to a worker pool draining SQS or RabbitMQ."
reading_time: 6
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

A plain [work queue](/articles/sys-patterns/2026-07-26-work-queue-pattern) treats every task as equal: FIFO, drained by interchangeable workers. That breaks the moment a payment confirmation and a nightly analytics job land in the same pipe. The **Priority Queue pattern** fixes the ordering: tasks carry a priority, and "requests with a higher priority are received and processed more quickly than those with a lower priority." Guaranteeing that the payment jumps ahead is the easy part. The part that bites you in production is what happens to the analytics job when payments never stop arriving.

## Two shapes, one guarantee

The Azure Architecture Center describes two implementations. The first is a **single priority-ordered queue**: one queue that "orders messages by priority, ensuring that consumers process higher-priority messages before lower-priority ones." Clean, but it needs a broker that supports priority ordering (RabbitMQ's `x-max-priority`, a database-backed queue with `ORDER BY priority`).

The second is **multiple queues**, one per priority level, and here the consumer topology is the real decision:

| Topology | How it works | Cost | Starvation risk |
|----------|--------------|------|-----------------|
| Single ordered queue | Broker sorts by priority | Low | High |
| Multi-queue, single consumer pool | Workers drain high queue first; touch low only when high is empty | Low | High |
| Multi-queue, multiple pools | Dedicated (bigger) pools per queue | Higher | Low — low-prio always gets *some* workers |

The single-pool model is the trap. "The single consumer pool always processes higher priority messages before lower priority ones" — which is exactly strict priority scheduling, and exactly how you starve the bottom of the queue.

## The failure mode: starvation

Strict priority has a well-known pathology. Azure states it plainly: this "could lead to lower priority messages being continually delayed and potentially never processed." As long as a high-priority message is available, the low-priority queue is never touched. Your analytics job isn't slow — it's stuck, indefinitely, and no error fires.

This is not a cloud problem; it's a 1960s problem. CPU schedulers hit it first, and their fixes are the canonical mitigations.

**Aging (priority boosting).** Raise a task's effective priority the longer it waits, so a starved item eventually outranks fresh arrivals. Azure's own recommendation — "dynamically increase the priority of aged messages" — is aging by another name. A Python worker pulling from an in-process heap can implement it directly. `heapq` is a min-heap, so *lower number = more urgent*, and we subtract a boost proportional to wait time:

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

A base-priority-5 task that has waited 12 seconds has effective priority `5 - 0.5*12 = -1`, so it now beats a freshly-arrived priority-0 task. The caveat: re-aging every element on each `pop` is O(n). Fine for a modest in-memory queue; for a large one, re-age lazily on a timer or push aged items up a level instead.

**Multi-level feedback queues (MLFQ).** The OS scheduler's answer, and the one worth stealing wholesale. OSTEP's rules: a new job "is placed in the highest priority queue"; "once a job uses up its time allotment at a level, its priority is reduced"; and — the anti-starvation rule — "after some time period S, move all the jobs in the system to the topmost queue." That last rule *is* aging applied to whole queues: a periodic priority boost. MLFQ also warns about **gaming** — a task that does a tiny bit of I/O to reset its slot and cling to a high priority — fixed by "better accounting" that tracks cumulative work regardless of voluntary yields. In a worker pool, the same abuse is a "high-priority" producer that floods the top queue; the boost period S and per-tenant accounting are your defenses.

**Weighted fair queueing (WFQ).** Instead of strict "high before low," give each priority a *share* of the workers — say 8:3:1 across three RabbitMQ queues. High priority gets most of the pool but low priority always keeps at least one consumer, so it drains slowly rather than never. This is the multi-pool row of the table, sized by weight.

## Choosing, honestly

Strict priority is simplest and correct *only* if low-priority work is genuinely optional — droppable telemetry, best-effort backfill. The instant a low-priority task has any deadline, you need aging or a weighted split, and you should alert on oldest-message age per priority so silent starvation becomes a page. And priority ordering does nothing for a total overload: if arrivals exceed capacity across all priorities, even the top queue backs up. Pair this pattern with **Queue-Based Load Leveling** to absorb bursts, and with autoscaling so the pool grows when the high-priority queue does.

**Try next:** Stand up two RabbitMQ queues, `high` and `low`, and a single worker that always drains `high` first. Fire a steady stream into `high` and watch `low` freeze at a non-zero depth forever. Then give `low` its own guaranteed consumer (an 8:1 weighted split) and confirm it drains — slowly but without starving.
