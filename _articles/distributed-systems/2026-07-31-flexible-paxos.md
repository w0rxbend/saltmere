---
title: "Flexible Paxos: your quorums only have to intersect across phases"
date: 2026-07-31
track: distributed-systems
summary: "Classic Paxos tells you to use majorities everywhere. Flexible Paxos shows that's stronger than you need: only the leader-election quorum and the replication quorum have to intersect. That one relaxation lets you make the common-case write cheaper."
reading_time: 5
tags: [paxos, consensus, quorums, replication, howard]
sources:
  - title: "Heidi Howard, Dahlia Malkhi, Alexander Spiegelman — Flexible Paxos: Quorum Intersection Revisited (arXiv:1608.06696, 2016)"
    url: "https://arxiv.org/abs/1608.06696"
  - title: "Flexible Paxos project page (fpaxos.github.io)"
    url: "https://fpaxos.github.io/"
  - title: "Flexible Paxos: Quorum Intersection Revisited — OPODIS 2016 (DROPS/LIPIcs)"
    url: "https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.OPODIS.2016.25"
---

Every consensus tutorial repeats the same rule: use a majority. To elect a leader you need a majority; to commit a value you need a majority; because two majorities of the same set always share a node, the new leader is guaranteed to see any value the old one committed. It's correct, it's simple, and — Heidi Howard, Dahlia Malkhi and Alexander Spiegelman showed in 2016 — it's stronger than Paxos actually requires.

## The quorums Paxos really needs to intersect

Paxos runs in two phases. **Phase 1** is leader election / prepare: a would-be leader contacts a quorum *Q1* and learns the highest-numbered value any of them has accepted. **Phase 2** is replication / accept: the leader asks a quorum *Q2* to accept its chosen value. The safety argument only ever relies on one intersection: **every Q1 must intersect every Q2**, so a new leader's phase-1 quorum overlaps the previous leader's phase-2 quorum and therefore sees its committed value.

What it does *not* require is that two phase-2 quorums intersect each other, or that two phase-1 quorums do. Majorities happen to give you all three intersections at once, which is why nobody noticed the extra two were unnecessary. Drop them and you get the Flexible Paxos rule:

> For a cluster of N nodes, any valid pair of quorum sizes works as long as **|Q1| + |Q2| > N**.

Majorities are just the special case |Q1| = |Q2| = ⌊N/2⌋ + 1.

## Why you'd want unequal quorums

Leader elections are rare; replication happens on every single write. So make the common case cheap by *shrinking Q2 and growing Q1*. Take N = 5:

| Scheme        | Q1 (elect) | Q2 (replicate) | Acks per write |
|---------------|-----------:|---------------:|---------------:|
| Classic Paxos |          3 |              3 |              3 |
| Flexible      |          4 |              2 |          **2** |

With Q2 = 2 you commit a write as soon as **two** nodes acknowledge it instead of three — lower tail latency, and you can keep writing even while three nodes are slow. The price is paid at election time: you now need 4 of 5 nodes up to elect a leader, so you tolerate fewer simultaneous failures *during a leadership change*. That's usually a good trade, because elections are infrequent and you control when your cluster is degraded.

The paper generalizes further: quorums don't even have to be simple counts. Arrange N nodes in a grid and let Q1 be "any full row" and Q2 be "any full column" — every row intersects every column, so the rule holds, and both quorums can be far smaller than a majority in a large grid.

## The one thing you can't forget

Because two phase-2 quorums may *not* intersect, a leader can no longer assume its own previous writes are visible to the next Q2 it talks to. Correctness rests entirely on the new leader running phase 1 against a proper Q1 before it starts accepting. In simple terms: **you may shrink the replication quorum, but the leader-election quorum has to make up the difference.** Systems that reuse a stable leader for many writes (Raft-style) are exactly where this pays off, as long as every leader change does a full, correctly-sized phase 1.

**Try next:** take a 5-node register you've built (or a toy Multi-Paxos), parameterize the accept path to require only 2 acks and the election path to require 4, and write a test that kills 3 nodes mid-run. Confirm writes still commit but a forced re-election stalls — that stall *is* the trade you just made, made visible.
