---
title: "Edge-chasing: detecting distributed deadlock without ever building the graph"
date: 2026-07-31
track: distributed-systems
summary: "The Chandy–Misra–Haas AND-model algorithm finds a cycle in a wait-for graph that no single node can see. Blocked processes chase probes along their wait edges; if a probe comes home, there's a deadlock — and you never assemble the full graph."
reading_time: 5
tags: [deadlock-detection, edge-chasing, chandy-misra-haas, wait-for-graph, coordination, distributed-systems]
sources:
  - title: "Chandy, Misra, Haas — Distributed Deadlock Detection (ACM TOCS, 1983)"
    url: "https://dl.acm.org/doi/10.1145/357360.357365"
  - title: "Chandy, Misra, Haas — Distributed Deadlock Detection (paper PDF)"
    url: "https://cse.iitkgp.ac.in/~agupta/distsys/Deadlock-ChandyMishraHaas.pdf"
  - title: "Kshemkalyani & Singhal — Distributed Computing, Ch. 10: Deadlock Detection in Distributed Systems"
    url: "https://www.cs.uic.edu/~ajayk/Chapter10.pdf"
  - title: "Wikipedia — Chandy–Misra–Haas algorithm (resource model)"
    url: "https://en.wikipedia.org/wiki/Chandy%E2%80%93Misra%E2%80%93Haas_algorithm_resource_model"
  - title: "van Steen & Tanenbaum — Distributed Systems (3rd ed.), coordination chapter"
    url: "https://www.distributed-systems.net/index.php/books/ds3/"
---

A deadlock is a cycle in the **wait-for graph** (WFG): process P1 blocked on a resource held by P2, P2 blocked on P3, ..., Pn blocked on P1. On one machine this is easy — the OS holds the whole graph and runs a cycle check. Spread the processes across nodes and the graph is spread with them: each node knows only its local slice of the edges. Nobody has the full picture, and the naive fix — ship everyone's edges to a coordinator — is worse than it looks.

## Why you can't just build the graph centrally

Send every local WFG to a coordinator and let it look for cycles. The problem is that the slices arrive at different times over links with "finite and unpredictable delay," and there is no global clock to line them up. The coordinator is always reasoning about a *stale* union of snapshots taken at different instants.

That produces **phantom (false) deadlocks**. Suppose the coordinator has an old edge P1→P2 (P1 waiting on P2) and a fresh edge P2→P1. In reality P2 released its resource and the P1→P2 edge is gone — no cycle ever existed simultaneously. But the coordinator's inconsistent snapshot contains both edges, sees a cycle, and aborts a process for nothing. A correct detector must never report deadlocks that don't exist, and stitching snapshots together across an asynchronous network can't guarantee that.

The same missing global clock kills **prevention** and **avoidance**. Prevention (grab all resources at once, or preempt) is wildly inefficient across a network; avoidance needs an accurate, real-time global state to test each grant for safety — exactly the thing you can't cheaply have. So in practice you let deadlocks happen and *detect* them.

## The model: AND requests

Chandy–Misra–Haas targets the **AND model**: a process may request several resources at once and stays blocked until *all* of them are granted. So a blocked process has an outgoing wait-for edge to every process it's waiting on, and it's stuck until every one of those clears. That's the common database-transaction case.

## Edge-chasing with probes

Instead of collecting the graph, CMH walks it with tiny messages called **probes**. A probe is a triple:

```
probe(i, j, k)   # i = initiator, j = sender, k = receiver
```

Read it as: "the deadlock hunt started by Pi has reached Pj, who is forwarding it to Pk because Pj is blocked waiting on Pk." Probes travel *only along wait-for edges*, and only *blocked* processes forward them — a running process isn't part of any cycle, so the chase stops there.

The detection rule is the whole trick: **when a process receives a probe whose initiator equals itself (`k == i`), the probe has traveled a full cycle of wait-for edges back to where it began — that cycle is a deadlock.** No node ever holds the cycle; the cycle reveals itself by the message coming home.

Here is the logic a blocked process runs on receiving a probe:

```python
class Process:
    def __init__(self, pid):
        self.pid = pid
        self.blocked = False
        self.waits_for = set()      # pids this process is blocked on (AND: all must clear)
        self.dependent = set()      # initiators we've already forwarded, to avoid re-sending

    def initiate(self):
        # Controller starts a hunt for a blocked process, using itself as initiator.
        if self.blocked:
            for k in self.waits_for:
                send(k, probe(self.pid, self.pid, k))

    def on_probe(self, i, j, k):     # k == self.pid: this probe was sent to us
        if not self.blocked:
            return                    # running process: chase dies here, no cycle through us

        if i == self.pid:
            declare_deadlock(i)       # probe came home -> cycle -> deadlock
            return

        if i in self.dependent:
            return                    # already propagated this initiator; prune duplicates
        self.dependent.add(i)

        for k2 in self.waits_for:     # forward along every outgoing wait-for edge
            send(k2, probe(i, self.pid, k2))
```

The `dependent` set is what makes this cheap and terminating: each blocked process forwards a given initiator's probe at most once per outgoing edge. The whole detection costs **at most `e` messages**, where `e` is the number of communicating (waiting) process pairs — for a chain of `n` blocked processes you send `O(n)` probes, never `O(n^2)`, and never the whole graph.

## Why not building the graph is the point

Edge-chasing sidesteps the coordinator's two failures at once. There's no central snapshot to go stale, so no phantom cycle from mismatched timestamps: a probe returns home *only if* a chain of currently-blocked processes actually links back to the initiator. And there's no bulk transfer of state — the "graph traversal" is distributed across the very nodes that own the edges, each doing an O(1) local step and passing a 3-tuple along. The algorithm reads a global property (a cycle) purely through local decisions and small messages, which is the recurring move in coordination: don't centralize the state, walk it.

**Try next:** implement the `Process` class above for 4 nodes and wire a cycle (P1→P2→P3→P4→P1). Confirm P1's probe returns to P1. Then, before it returns, have P4 "release" and drop the P4→P1 edge, and watch the chase die with no false alarm — the exact phantom deadlock a stale centralized snapshot would have wrongly reported.
