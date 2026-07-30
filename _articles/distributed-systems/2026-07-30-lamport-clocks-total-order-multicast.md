---
title: "Lamport Clocks and Total-Order Multicast: agreeing on order without a clock"
date: 2026-07-30
track: distributed-systems
summary: "Scalar Lamport timestamps give you a consistent 'happened-before' order from nothing but message counters. Add process-id tie-breaking and acknowledgements and you get total-order multicast — the backbone of replicated state machines. Here's the algorithm and a runnable Python core."
reading_time: 6
tags: [lamport-clocks, logical-clocks, happened-before, total-order-multicast, state-machine-replication]
sources:
  - title: "Time, Clocks, and the Ordering of Events in a Distributed System (CACM 1978) — Leslie Lamport"
    url: "https://lamport.azurewebsites.net/pubs/time-clocks.pdf"
  - title: "Distributed Systems (4th ed.) — van Steen & Tanenbaum, Ch. 6 Coordination"
    url: "https://www.distributed-systems.net/index.php/books/ds4/"
  - title: "Implementing Fault-Tolerant Services Using the State Machine Approach (ACM Computing Surveys 1990) — Fred Schneider"
    url: "https://www.cs.cornell.edu/fbs/publications/smsurvey.pdf"
  - title: "Distributed Systems 6.4: Total order broadcast (lecture notes) — Martin Kleppmann"
    url: "https://www.cl.cam.ac.uk/teaching/2122/ConcDisSys/dist-sys-notes.pdf"
---

You cannot trust wall clocks in a distributed system: they drift, they jump when NTP corrects them, and no two are ever exactly equal. Yet you constantly need to answer "did A happen before B?" Lamport's 1978 insight was that for *ordering*, you don't need real time at all — you need a counter that respects causality. That counter is a **logical clock**, and it's the cheapest useful primitive in distributed systems.

## Happened-before

Lamport defines a partial order `→` ("happened-before") over events:

1. If `a` and `b` are in the same process and `a` comes first, then `a → b`.
2. If `a` is the sending of a message and `b` is its receipt, then `a → b`.
3. Transitivity: if `a → b` and `b → c`, then `a → c`.

If neither `a → b` nor `b → a`, the events are **concurrent** — genuinely unordered, and no amount of cleverness will order them "correctly," because there is no correct answer. (Vector clocks, covered in its own article here, can *detect* this concurrency; scalar Lamport clocks cannot.)

## The scalar clock

Each process keeps one integer, `C`. Two rules maintain it:

- **Before any event** (local step or a send), increment: `C := C + 1`.
- **On send**, attach the current `C` as the message timestamp `t`.
- **On receive** of a message with timestamp `t`: `C := max(C, t) + 1`.

The guarantee (the "clock condition"): if `a → b` then `C(a) < C(b)`. Note the arrow only goes one way — `C(a) < C(b)` does **not** imply `a → b`. That's the fundamental limitation of a scalar clock, and it's fine, because for total order we don't need the converse.

```python
class LamportClock:
    def __init__(self):
        self.c = 0
    def local_event(self):
        self.c += 1
        return self.c
    def on_send(self):
        self.c += 1
        return self.c            # stamp the outgoing message with this
    def on_receive(self, t):
        self.c = max(self.c, t) + 1
        return self.c
```

## From partial order to total order

`→` leaves concurrent events unordered, but sometimes every process must agree on *one* order — for example, so that replicas applying the same operations end up in the same state. We build a **total order** by breaking ties: order events by `(timestamp, process_id)`. Since process ids are unique, no two events tie, and every process computes the *same* order. It's arbitrary for concurrent events, but it's *consistent* — that's all replication needs.

## Total-order multicast

Now the payoff. **Total-order (atomic) multicast** delivers messages to all group members in the *same* order everywhere. Lamport's classic algorithm builds it from logical clocks plus a per-process priority queue, assuming FIFO, reliable channels:

1. To multicast a message, a process timestamps it with its Lamport clock and sends it to **everyone, including itself**. Each receiver puts it in a local queue ordered by `(timestamp, sender_id)` and replies with a timestamped **ack** to all.
2. A process **delivers** (acts on) the message at the head of its queue only when: (a) that message is acknowledged by *all* members, and (b) it has the smallest `(timestamp, sender_id)` in the queue.

Condition (b) is the subtle one. Because channels are FIFO and every process floods acks, once a message `m` is fully acked, *no future message from any process can have a smaller timestamp* — any later message must have seen a clock at least as large. So when `m` reaches the head with all acks in, it is safe to deliver, and every process reaches this state for `m` in the same relative order. Everyone delivers in `(timestamp, sender_id)` order. Total order achieved with no central sequencer.

```python
import heapq

class TotalOrderNode:
    def __init__(self, pid, peers):
        self.pid, self.peers = pid, peers
        self.clock = LamportClock()
        self.queue = []          # heap of (ts, sender, msg_id, msg)
        self.acks = {}           # msg_id -> set of pids that acked

    def multicast(self, msg):
        ts = self.clock.on_send()
        mid = (ts, self.pid)
        for p in self.peers:     # peers includes self
            p.deliver_msg(self.pid, ts, mid, msg)

    def deliver_msg(self, sender, ts, mid, msg):
        self.clock.on_receive(ts)
        heapq.heappush(self.queue, (ts, sender, mid, msg))
        self.acks.setdefault(mid, set())
        ack_ts = self.clock.on_send()
        for p in self.peers:
            p.deliver_ack(self.pid, ack_ts, mid)
        self._try_deliver()

    def deliver_ack(self, from_pid, ts, mid):
        self.clock.on_receive(ts)
        self.acks.setdefault(mid, set()).add(from_pid)
        self._try_deliver()

    def _try_deliver(self):
        while self.queue:
            ts, sender, mid, msg = self.queue[0]
            if len(self.acks.get(mid, ())) < len(self.peers):
                return           # head not fully acked yet — must wait
            heapq.heappop(self.queue)
            self.apply(msg)      # deliver in agreed total order
```

## Why this matters: replicated state machines

Total-order multicast is the delivery layer under **state-machine replication** (Schneider 1990): if every replica starts in the same state and applies the *same* deterministic operations in the *same* order, they stay identical. Consensus protocols like Raft and Multi-Paxos exist largely to implement exactly this — an agreed, gap-free order of operations — but with crash *and* partition tolerance that Lamport's FIFO-reliable version assumes away. Understanding the clock-based version first makes the consensus machinery feel much less magical: it's total-order multicast that also survives lost messages and minority failures.

**Try next:** Run the two classes above with three in-process nodes and have two of them `multicast` concurrently. Log the delivery order at each node and assert all three deliver in identical order. Then deliberately reorder one node's ack handling (break FIFO) and watch a divergent delivery order appear — a hands-on demonstration of exactly which assumption the algorithm leans on.
