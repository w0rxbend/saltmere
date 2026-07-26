---
title: "Hybrid Logical Clocks: timestamps that stay close to the wall clock and still capture causality"
date: 2026-07-26
track: distributed-systems
summary: "Lamport clocks capture causality but mean nothing on a wall; NTP-synced clocks mean something but can go backwards relative to causal order. HLC is the O(1)-size fix both CockroachDB and MongoDB ship in production."
reading_time: 5
tags: [hybrid-logical-clocks, logical-clocks, ntp, causality, cockroachdb, mongodb, coordination]
sources:
  - title: "Kulkarni, Demirbas, Madappa, Avva, Leone — Logical Physical Clocks and Consistent Snapshots in Globally Distributed Databases (2014)"
    url: "https://cse.buffalo.edu/tech-reports/2014-04.pdf"
  - title: "CockroachDB Docs — Transaction Layer (HLC, clock offset, uncertainty intervals)"
    url: "https://www.cockroachlabs.com/docs/stable/architecture/transaction-layer"
  - title: "CockroachDB Glossary — Hybrid Logical Clock (HLC) Timestamps"
    url: "https://www.cockroachlabs.com/glossary/distributed-db/hybrid-logical-clock-hlc-timestamps/"
  - title: "Tyulenev et al. — Implementation of Cluster-wide Logical Clock and Causal Consistency in MongoDB (SIGMOD 2019)"
    url: "https://dl.acm.org/doi/10.1145/3299869.3314049"
  - title: "Sookocheff — Hybrid Logical Clocks"
    url: "https://sookocheff.com/post/time/hybrid-logical-clocks/"
---

The vector-clocks article here covered the classic answer to "did A happen before B?": attach `O(N)` counters and compare vectors. That answer is exact but tells you nothing about *when*, in the wall-clock sense, anything happened, and it doesn't scale to the number of nodes in a database cluster. HLC solves a narrower, more practical problem: give every event a timestamp that (1) is close to physical time, (2) respects happens-before, and (3) costs one 64-bit-ish word, not a vector. It's the clock CockroachDB and MongoDB actually run.

## Two failing baselines

**Pure Lamport clocks.** A single integer per process, bumped on every event and on message receipt (`max(local, received) + 1`), guarantees `A → B ⟹ L(A) < L(B)`. But `L(A)` has no relationship to real time — you cannot look at `L(A) = 47` and ask "was this within my 10-second read window?" or compare it to a timestamp from a system that never saw a Lamport message. It's causally correct and temporally useless.

**Pure physical clocks (NTP).** `time.Now()` on every node is meaningful (roughly synced to UTC, usually within milliseconds via NTP) but breaks causality guarantees. Clock skew — NTP drift, virtualization jitter, leap-second handling — means a later event can legitimately get an *earlier* timestamp than a causally preceding one from another node. Snapshot reads and "give me everything before T" queries silently miss data, and last-writer-wins resolution picks the wrong writer.

HLC (Kulkarni, Demirbas, Madappa, Avva, and Leone, 2014) fuses the two: it is provably isomorphic to a Lamport clock (so it captures causality exactly the way Lamport clocks do) while staying within a bounded distance of physical time.

## The HLC algorithm

Each node keeps a pair `(l, c)`: `l` is the highest physical time it has observed (from itself or from a message), and `c` is a logical counter that only advances when two events tie on `l`. `pt` is the node's local NTP-disciplined physical clock.

| Event | Rule |
|---|---|
| Local / send | `l' = l; l = max(l, pt); c = c+1 if l == l' else 0` |
| Receive `(l_m, c_m)` from message | `l' = l; l = max(l, l_m, pt)`; then: if all three of `l, l', l_m` tie, `c = max(c, c_m)+1`; if only `l == l'`, `c = c+1`; if only `l == l_m`, `c = c_m+1`; else `c = 0` |

The invariant the paper proves is `l.e ≥ pt.e` for every event `e`: the logical part never falls behind the physical clock, and it only runs ahead by the amount of clock skew actually observed in messages — bounded, in practice, by NTP's own error bars (single-digit milliseconds in a well-run cluster, though the paper's bound is stated in terms of the maximum clock skew `ε` between any two nodes). Comparisons are lexicographic on `(l, c)`, giving a single 64-ish-bit value that sorts the same way a `(timestamp, tie-breaker)` pair would, but with a causality guarantee attached.

```python
import time

class HLC:
    def __init__(self):
        self.l = 0   # highest physical time observed (ms)
        self.c = 0   # logical counter for ties

    def _pt(self):
        return int(time.time() * 1000)

    def send_or_local(self):
        pt = self._pt()
        l_prev = self.l
        self.l = max(self.l, pt)
        self.c = self.c + 1 if self.l == l_prev else 0
        return (self.l, self.c)

    def recv(self, l_msg, c_msg):
        pt = self._pt()
        l_prev = self.l
        self.l = max(self.l, l_msg, pt)
        if self.l == l_prev == l_msg:
            self.c = max(self.c, c_msg) + 1
        elif self.l == l_prev:
            self.c = self.c + 1
        elif self.l == l_msg:
            self.c = c_msg + 1
        else:
            self.c = 0
        return (self.l, self.c)
```

Send a `(l, c)` pair on every outbound message, merge on every inbound one, and any two timestamps you compare give you both an approximate wall-clock reading and a correct happens-before order — the same guarantee a Lamport clock gives, plus a real-time anchor.

## What this buys a database

**CockroachDB** stamps every transaction with an HLC timestamp and uses it as the MVCC version and the transaction's read/commit timestamp. Because `l` is always ≥ physical time, a node can bound "how uncertain am I about what's concurrent with me right now" by its configured `max_offset` (500ms by default) — this is the basis of CockroachDB's *uncertainty interval*: a read that finds a value timestamped within the uncertainty window pushes its own timestamp forward instead of silently returning a stale-looking result. CockroachDB also treats clock skew as a correctness-critical parameter — a node that detects skew exceeding 80% of `max_offset` against a majority of peers crashes itself rather than risk violating single-key linearizability.

**MongoDB** (since 3.6) uses a cluster-wide logical clock for causal consistency: every op gets a `ClusterTime`, and the value returned to a client after a write, `operationTime`, is passed back on subsequent reads so a secondary can wait until it has replicated at least that far before answering. It's Lamport-clock causality tracking bound to physical time the same HLC way, which is what makes "read-your-own-writes across a replica set, without pinning to the primary" possible via causally consistent sessions plus `majority` read/write concerns.

## Comparing the three

| Property | Lamport clock | Physical clock (NTP) | HLC |
|---|---|---|---|
| Captures happens-before | Yes | No | Yes |
| Close to wall-clock time | No | Yes | Yes (bounded by skew) |
| Size | O(1) | O(1) | O(1) |
| Detects concurrency exactly | No (vector clocks do) | No | No (same limit as Lamport) |
| Used by | textbook algorithms | naive `LWW` systems | CockroachDB, MongoDB |

Note the row that doesn't change: HLC is exactly as bad as a Lamport clock at telling concurrent events apart from causally-ordered ones — it inherits that limitation. If you need to *detect* concurrent writes (not just order all events consistently), you still want a vector clock or a dotted version vector layered on top, at O(N) cost. HLC's whole pitch is being the practical middle ground for total ordering plus timestamps that mean something, at a price a database can actually afford to attach to every row.

**Try next:** wire the `HLC` class above into two toy "nodes" exchanging messages over a socket or queue, deliberately skew one node's system clock, and watch `recv()` absorb the skew into `c` instead of producing an out-of-order timestamp.
