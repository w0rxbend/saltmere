---
title: "Decoding Distributed Time: which clock to reach for when there is no global one"
date: 2026-08-10
track: distributed-systems
summary: "Physical clocks, Lamport, vector clocks, HLC, and TrueTime all answer 'what order did things happen in?' — but they capture different things at different costs. This is the side-by-side comparison and a decision guide for picking one, with the happens-before guarantees stated precisely."
reading_time: 7
tags: [logical-clocks, lamport-clocks, vector-clocks, hybrid-logical-clocks, truetime, causality, happens-before]
sources:
  - title: "Lamport — Time, Clocks, and the Ordering of Events in a Distributed System (CACM 1978)"
    url: "https://lamport.azurewebsites.net/pubs/time-clocks.pdf"
  - title: "Kulkarni, Demirbas, Madappa, Avva, Leone — Logical Physical Clocks and Consistent Snapshots in Globally Distributed Databases (2014)"
    url: "https://cse.buffalo.edu/tech-reports/2014-04.pdf"
  - title: "Corbett et al. — Spanner: Google's Globally-Distributed Database (OSDI 2012)"
    url: "https://www.usenix.org/system/files/conference/osdi12/osdi12-final-16.pdf"
  - title: "Demirbas — Use of Time in Distributed Databases (part 4): Synchronized clocks in production databases"
    url: "http://muratbuffalo.blogspot.com/2025/01/use-of-time-in-distributed-databases.html"
  - title: "Almeida, Baquero, Fonte — Interval Tree Clocks: A Logical Clock for Dynamic Systems (OPODIS 2008)"
    url: "https://gsd.di.uminho.pt/members/cbm/ps/itc2008.pdf"
---

There is no global clock in a distributed system. Every node has its own crystal oscillator drifting at its own rate, and no amount of network syncing makes them agree exactly. Yet nearly every hard problem — snapshots, MVCC, conflict resolution, replicated logs, "read your own writes" — reduces to ordering events across machines. The journal already has deep-dives on each mechanism individually; this article is the map that sits above them. It contrasts the five practical answers, states exactly what each can and can't do, and gives you a decision rule.

The whole subject rests on one definition, so get it exact. Lamport's **happens-before** (`→`) is the smallest relation such that: (1) if `a` and `b` are on the same process and `a` comes first, then `a → b`; (2) if `a` is a send and `b` is the matching receive, then `a → b`; (3) it's transitive. If neither `a → b` nor `b → a`, the events are **concurrent** (`a ∥ b`). Everything below is a strategy for encoding this partial order into timestamps you can compare.

## The five mechanisms

### Physical clocks and NTP

Read `time.now()`, sync it to UTC with NTP, and stamp events. The appeal is that the number *means* something — it's roughly wall-clock time, comparable to timestamps from systems that never exchanged a message with you. The fatal flaw for ordering: physical clocks violate causality. NTP corrects drift by stepping the clock, which can jump it **backwards**; virtualization jitter, leap-second smearing, and skew between nodes all mean a causally *later* event can get a *smaller* timestamp than the event that caused it. Last-writer-wins on wall-clock time silently picks the wrong writer, and a "give me everything before T" snapshot silently drops data. Physical time is meaningful but not monotone with respect to `→`. See the [clock-synchronization deep-dive](/articles/distributed-systems/2026-07-30-clock-synchronization-cristian-ntp) for how Cristian, Berkeley, and NTP bound — but never eliminate — the error.

### Lamport (scalar) logical clocks

One integer `L` per process: increment on every local event and send; on receive, `L = max(L, L_msg) + 1`. This guarantees the forward implication and *only* the forward implication:

> `a → b ⟹ L(a) < L(b)`. The converse is false: `L(a) < L(b)` does **not** imply `a → b` — they may be concurrent.

So a Lamport clock gives you a **total order consistent with causality** (break ties with process ID), which is exactly what state-machine replication and total-order multicast need. What it cannot do is *detect* concurrency: given two timestamps, you can't tell whether one caused the other or they were independent. And `L = 47` has no relation to any wall. See [Lamport clocks and total-order multicast](/articles/distributed-systems/2026-07-30-lamport-clocks-total-order-multicast).

### Vector clocks / version vectors

Keep a vector `V` of one counter per node. Increment your own entry on each event; on receive, take the element-wise max and then bump your own. Now comparison is exact: `a → b` iff `V(a) < V(b)` element-wise, and if neither dominates the other they are genuinely concurrent. This is the only mechanism here that **detects concurrency** — which is why Dynamo-style stores attach version vectors to values to flag conflicting writes for the application (or a CRDT) to reconcile. The cost is `O(N)` size per timestamp for `N` participants, and it assumes a stable, known set of node IDs. See [vector clocks in ~40 lines](/articles/distributed-systems/2026-07-24-vector-clocks-in-40-lines).

### Hybrid Logical Clocks (HLC)

HLC (Kulkarni, Demirbas, et al., 2014) fuses the two scalar approaches. Each node keeps a pair `(l, c)`: `l` is the highest physical time it has observed (its own or from a message), and `c` is a logical counter that only advances when events tie on `l`. The paper proves HLC is isomorphic to a Lamport clock — so it captures happens-before exactly as well — while the invariant `l.e ≥ pt.e` keeps `l` within a bounded distance (the max clock skew `ε`) of physical time. You get Lamport's causality guarantee *plus* a timestamp that reads like a wall clock, at `O(1)` size. It is the modern practical default: CockroachDB and MongoDB both ship it. Like Lamport, HLC cannot detect concurrency — that limitation is inherited, not fixed. See [Hybrid Logical Clocks](/articles/distributed-systems/2026-07-26-hybrid-logical-clocks).

Here is the full update/send/receive rule — the piece worth memorizing:

```python
import time

class HLC:
    def __init__(self):
        self.l = 0   # highest physical time observed (ms)
        self.c = 0   # logical tie-breaker counter

    def _pt(self):
        return int(time.time() * 1000)

    def send_or_local(self):            # local event or outbound msg
        l_prev = self.l
        self.l = max(self.l, self._pt())
        self.c = self.c + 1 if self.l == l_prev else 0
        return (self.l, self.c)

    def recv(self, l_msg, c_msg):       # inbound msg carries (l, c)
        l_prev, pt = self.l, self._pt()
        self.l = max(l_prev, l_msg, pt)
        if self.l == l_prev == l_msg:
            self.c = max(self.c, c_msg) + 1
        elif self.l == l_prev:
            self.c = self.c + 1
        elif self.l == l_msg:
            self.c = c_msg + 1
        else:
            self.c = 0                   # physical time advanced past both
        return (self.l, self.c)
```

Comparison is lexicographic on `(l, c)`, so a single ~64-bit value sorts the same way a `(timestamp, tie-breaker)` pair would — but with a causality guarantee attached.

### TrueTime (Google Spanner)

Spanner takes the opposite bet: make physical time trustworthy with **hardware**. GPS receivers and atomic clocks in every datacenter let TrueTime's `TT.now()` return an *interval* `[earliest, latest]` guaranteed to contain the true time, with uncertainty `ε` of roughly 6 ms in the 2012 paper (better since). The key move is **commit-wait**: after picking a commit timestamp `s`, a transaction deliberately waits until `TT.now().earliest > s` — it sleeps out the uncertainty (about `2ε`) before releasing its locks and making writes visible. That wait guarantees that any transaction which starts later in real time gets a strictly larger timestamp, delivering **external consistency** (strict serializability) across continents. The price is the wait latency and a GPS/atomic-clock deployment most teams don't have. See [TrueTime and commit-wait](/articles/distributed-systems/2026-07-31-spanner-truetime-external-consistency).

### Briefly: Interval Tree Clocks

Vector clocks assume a fixed, known membership. ITCs (Almeida, Baquero, Fonte, 2008) make the ID space itself divisible: a node can **fork** a fresh identity locally when it joins and **join** it back on exit, so metadata grows and shrinks with the cluster instead of monotonically. Think "vector clocks for dynamic membership." See [Interval Tree Clocks](/articles/distributed-systems/2026-07-30-interval-tree-clocks).

## The comparison table

| | Physical / NTP | Lamport | Vector clock | HLC | TrueTime | ITC |
|---|---|---|---|---|---|---|
| Captures happens-before (`a→b ⟹ ts↑`) | No | Yes | Yes | Yes | Yes (via wait) | Yes |
| Detects concurrency (`a∥b`) | No | No | **Yes** | No | No | **Yes** |
| Wall-clock-ish timestamp | Yes | No | No | **Yes** (±ε) | **Yes** (±ε) | No |
| Size per timestamp | O(1) | O(1) | **O(N)** | O(1) | O(1) | O(events), grows/shrinks |
| Needs special hardware | No (NTP) | No | No | No | **Yes** (GPS/atomic) | No |
| Handles dynamic membership | n/a | Weakly | No (fixed IDs) | Yes | Yes | **Yes** |
| Total order across all events | No | Yes (+ tie-break) | Partial only | Yes (+ tie-break) | Yes | Partial only |
| Typical use | logging, coarse LWW | SMR, total-order multicast | Dynamo conflict detection | CockroachDB, MongoDB | Spanner | dynamic P2P / CRDT metadata |

## A decision guide

- **You need a total order for a replicated log / state machine, causality is enough, wall-clock meaning is irrelevant** → **Lamport clock**. Cheapest thing that works.
- **You need to detect conflicting concurrent writes** (siblings, LWW-is-wrong, CRDT merge) **with a fixed node set** → **Vector clock / version vector**.
- **Same, but nodes join and leave constantly** and you can't afford ever-growing per-node metadata → **Interval Tree Clocks**.
- **You need a total order AND timestamps that mean something on a wall** (MVCC versions, snapshot reads, causal sessions) **on commodity hardware** → **HLC**. This is the pragmatic default for a modern distributed database.
- **You need external consistency / strict serializability across datacenters** and can deploy GPS + atomic clocks → **TrueTime + commit-wait**.
- **You only need approximate ordering for humans** (log lines, dashboards) and never for correctness → **plain NTP** — just never make a *decision* on it.

The through-line: pick the weakest mechanism that captures the property you actually depend on. Concurrency detection costs you `O(N)` metadata; wall-clock-accurate external consistency costs you hardware and wait latency. If you need neither, a scalar clock is the whole answer — which is why Lamport's 1978 paper is still the first thing to read.

**Try next:** take the `HLC` class above, run two instances exchanging messages, and step one node's system clock backwards mid-run — watch `recv()` absorb the skew into `c` and keep timestamps monotone, then swap in a vector clock and confirm it's the only one that flags the two writes as concurrent.
