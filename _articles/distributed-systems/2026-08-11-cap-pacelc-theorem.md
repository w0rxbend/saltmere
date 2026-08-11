---
title: "CAP and PACELC: the interview version, stated precisely"
date: 2026-08-11
track: distributed-systems
summary: "\"Pick 2 of 3\" is the wrong mental model for CAP, and it stops at the wrong question. Here's the precise Gilbert–Lynch statement, why P is not a choice, and how PACELC's else-clause classifies Cassandra, HBase, and Spanner in one line each."
reading_time: 5
tags: [cap-theorem, pacelc, consistency, availability, latency]
sources:
  - title: "Gilbert & Lynch, Brewer's Conjecture and the Feasibility of Consistent, Available, Partition-Tolerant Web Services (ACM SIGACT News 33:2, 2002)"
    url: "https://www.cs.princeton.edu/courses/archive/spr22/cos418/papers/cap.pdf"
  - title: "Abadi, Consistency Tradeoffs in Modern Distributed Database System Design (IEEE Computer, Feb 2012)"
    url: "https://www.cs.umd.edu/~abadi/papers/abadi-pacelc.pdf"
  - title: "Brewer, CAP Twelve Years Later: How the \"Rules\" Have Changed (IEEE Computer, Feb 2012)"
    url: "https://sites.cs.ucsb.edu/~rich/class/cs293b-cloud/papers/brewer-cap.pdf"
  - title: "Abadi, Problems with CAP, and Yahoo's little known NoSQL system (DBMS Musings, 2010)"
    url: "https://dbmsmusings.blogspot.com/2010/04/problems-with-cap-and-yahoos-little.html"
---

Eric Brewer stated CAP as a conjecture in his PODC 2000 keynote. Two years later Seth Gilbert and Nancy Lynch turned it into a theorem with an actual proof (ACM SIGACT News, 2002). The interview trap is that most people can recite the "pick 2 of 3" cartoon and almost none can state the three words precisely. The words are the whole point.

## What C, A, and P actually mean

In the Gilbert–Lynch model these are formal properties, not vibes:

- **Consistency (C)** means *linearizability* (they call it atomic): there is a single total order on operations such that each looks as if it took effect instantaneously at some point between its call and return. A read returns the most recent completed write. This is the strongest single-object model — see the [sequential/causal consistency](/articles/distributed-systems/sequential-causal-consistency-models) article for the weaker cousins.
- **Availability (A)** means *every request to a non-failing node eventually returns a non-error response*. Note "eventually" — it says nothing about latency. A node that answers after ten minutes is still "available" by this definition.
- **Partition tolerance (P)** means the system keeps its guarantees even when the network *drops arbitrarily many messages* between two groups of nodes.

## Why "pick 2 of 3" is misleading

P is not a feature you choose. Networks partition — links fail, switches reboot, a GC pause looks exactly like a dead node. You do not get to opt out of packet loss, so a real distributed system must tolerate partitions. The genuine choice only appears *during* a partition, and it is binary:

- Keep answering on both sides and risk divergence → sacrifice **C**, keep **A**. (**AP**)
- Refuse to answer on the minority side to stay consistent → sacrifice **A**, keep **C**. (**CP**)

So CAP is really "when partitioned, choose C or A." The proof is a short adversary argument: partition the network into `{G1}` and `{G2}`, write `v2` on `G1`, then read on `G2`. If the read must return (availability) and cannot see `v2` (no messages cross), it returns stale `v1`, breaking linearizability. Something has to give. The only system that "picks CA" is one that isn't partition-tolerant — i.e. a single node, or one that simply breaks when the network does.

Brewer's own 2012 retrospective ("CAP Twelve Years Later") makes the same correction: the trade-off is not a one-time architectural pick but a per-operation decision made only when a partition is detected, and good systems restore consistency afterward.

## PACELC: the else-clause CAP forgot

CAP is silent about the common case — no partition — where a real trade-off still exists. Daniel Abadi closed that gap with **PACELC** (IEEE Computer, 2012):

> **If** there is a **P**artition, trade off **A**vailability vs **C**onsistency; **E**lse, trade off **L**atency vs **C**onsistency.

The else-clause is the insight. Even with a healthy network, keeping replicas linearizable means a write must reach a quorum before it acknowledges, and that round trip costs latency (see [quorum replication](/articles/distributed-systems/quorum-replication-r-plus-w)). A system that answers from the nearest replica is faster but may serve stale data. So you classify a system twice: `PA` or `PC`, then `EL` or `EC`.

| System | PACELC | Reading |
|---|---|---|
| Dynamo, Cassandra, Riak | **PA/EL** | Stay available under partition; favor latency (nearest replica, low `R`/`W`) otherwise |
| PNUTS (Yahoo) | **PC/EL** | Consistent under partition; but favors latency in normal operation |
| MongoDB | **PA/EC** | Primary failover can lose acks under partition; consistent reads from primary otherwise |
| HBase, BigTable, VoltDB | **PC/EC** | Never sacrifice consistency; pay in availability and latency |
| Spanner | **PC/EC** | Externally consistent via [TrueTime](/articles/distributed-systems/spanner-truetime-external-consistency) commit-wait; a CP system that is EC almost always |

Spanner is the instructive one. It is unambiguously **PC** — on partition it stops serving the minority side rather than diverge. Its marketing "five nines" availability doesn't violate CAP; Google's network just partitions rarely enough that the CP cost is seldom paid. Abadi's point stands: it's still `PC/EC`.

## A worked example: reading PACELC off a quorum config

PACELC labels aren't fixed per product — for tunable stores they're a config decision. Cassandra with `N=3` replicas:

```yaml
# EC end: writes and reads intersect (R + W > N), linearizable-ish reads
replication_factor: 3
write_consistency: QUORUM   # W = 2
read_consistency:  QUORUM   # R = 2   ->  R + W = 4 > 3, overlap guaranteed

# EL end: same cluster, favor latency, accept staleness
write_consistency: ONE      # W = 1
read_consistency:  ONE      # R = 1   ->  R + W = 2 < 3, may read stale
```

The first block buys `EC`: every read set intersects the last write set, so you don't return stale data — at the cost of waiting for two nodes. The second buys `EL`: one-node round trips, lowest latency, no overlap guarantee. Same binary shows up under partition: with `QUORUM` writes, the minority side (1 of 3 nodes) can't reach `W=2` and *refuses* writes — that's leaning `PC`. With `ONE`, the lone node keeps accepting — that's `PA`. One knob moves you across both halves of PACELC.

## The interview-ready summary

State it in three sentences. CAP: partitions are a given, so under a partition you choose availability or linearizability, never both. The "pick 2 of 3" phrasing is wrong because P isn't optional. PACELC adds the missing case: even with no partition you still trade latency against consistency, which is why two `CP` databases can behave very differently day to day.

**Try next:** take a store you actually run and write its PACELC label, then justify each letter with a specific config value (quorum size, `maxStalenessSeconds`, read-preference) — if you can't point at the knob, you don't yet know where it sits.
