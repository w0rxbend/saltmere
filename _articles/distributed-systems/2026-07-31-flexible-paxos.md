---
title: "Flexible Paxos: quorums only have to intersect across phases"
date: 2026-07-31
track: distributed-systems
summary: "Classic Paxos prescribes majorities everywhere. Flexible Paxos shows that requirement is stronger than necessary: only the leader-election quorum and the replication quorum must intersect. The relaxation makes the common-case write cheaper and leader election more expensive."
reading_time: 6
tags: [paxos, consensus, quorums, replication, howard]
sources:
  - title: "Heidi Howard, Dahlia Malkhi, Alexander Spiegelman — Flexible Paxos: Quorum Intersection Revisited (arXiv:1608.06696, 2016)"
    url: "https://arxiv.org/abs/1608.06696"
  - title: "Flexible Paxos project page (fpaxos.github.io)"
    url: "https://fpaxos.github.io/"
  - title: "Flexible Paxos: Quorum Intersection Revisited — OPODIS 2016 (DROPS/LIPIcs)"
    url: "https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.OPODIS.2016.25"
---

**Gist.** Textbook Paxos requires a majority of nodes in both of its phases, which makes every write wait for ⌊N/2⌋ + 1 acknowledgements. Howard, Malkhi and Spiegelman (2016) established that safety depends on a single intersection — every phase-1 (leader-election) quorum must intersect every phase-2 (replication) quorum — so any sizes satisfying **|Q1| + |Q2| > N** are admissible. The cost of shrinking the replication quorum is that the election quorum grows by the same amount, so fewer simultaneous failures can be tolerated during a leadership change.

## The intersection the safety argument uses

Paxos runs in two phases. **Phase 1 (prepare)** is leader election: a would-be leader picks a proposal number, contacts a quorum *Q1*, and learns the highest-numbered value any member of that quorum has already accepted. If such a value exists, the leader is obliged to re-propose it rather than its own. **Phase 2 (accept)** is replication: the leader asks a quorum *Q2* to accept the chosen value, and the value is committed once every member of some Q2 has accepted.

The safety property — no two proposals commit different values — is carried by one step of the argument. A new leader's phase-1 quorum must contain at least one acceptor that participated in the committed phase-2 quorum of the previous leader, because that acceptor is what reports the committed value back and forces the new leader to re-propose it. In other words the requirement is **every Q1 intersects every Q2**, and nothing more.

Two intersections that classic Paxos also provides are never invoked: **Q2 ∩ Q2 ≠ ∅** and **Q1 ∩ Q1 ≠ ∅**. Majorities supply all three at once, which is why the redundancy went unremarked. Discarding the two unused ones yields the Flexible Paxos rule:

> For a cluster of N nodes, any pair of quorum sizes is admissible provided **|Q1| + |Q2| > N**.

Majorities are the special case |Q1| = |Q2| = ⌊N/2⌋ + 1, where the sum is N + 1 or N + 2 depending on the parity of N.

## Unequal quorums and what they buy

Leader elections are infrequent; replication occurs on every write. The asymmetry can therefore be spent by shrinking Q2 and growing Q1. For N = 5:

| Scheme        | Q1 (elect) | Q2 (replicate) | Acks per write |
|---------------|-----------:|---------------:|---------------:|
| Classic Paxos |          3 |              3 |              3 |
| Flexible      |          4 |              2 |          **2** |

With |Q2| = 2 a write commits after **two** acknowledgements rather than three. The commit latency of a write becomes the second-fastest acceptor's response rather than the third-fastest, and progress continues while three of the five acceptors are slow or unreachable, provided the leader can still reach two. The corresponding loss is at election time: **4 of the 5 acceptors must be reachable to complete phase 1**, so at most one failure is tolerable during a leadership change, against two under majorities.

The paper generalises beyond counting quorums. Arranging N nodes in a grid and defining Q1 as any full row and Q2 as any full column satisfies the rule, because every row intersects every column. In a large grid both quorums are substantially smaller than a majority; the intersection requirement is met by the geometry rather than by cardinality.

## The invariant that must not be dropped

Because two phase-2 quorums need not intersect, **a leader can no longer assume that a quorum it is about to write to has seen its own earlier writes**. Under majorities that assumption is free and implementations quietly rely on it. Under |Q1| + |Q2| > N with |Q2| below a majority it is false: with N = 5 and |Q2| = 2, the sets {a, b} and {c, d} are both valid replication quorums and are disjoint.

Correctness therefore rests entirely on **every new leader completing phase 1 against a full, correctly sized Q1 before accepting anything**. The election quorum is what recovers the history that the small replication quorums scattered. Systems that keep a stable leader across many writes are where the arrangement pays, because the expensive phase runs once per leadership term while the cheap phase runs once per command.

The failure mode of getting this wrong is silent. A leader that skips or truncates phase 1, or that is configured with a Q1 sized for majorities while Q2 has been shrunk, may miss a committed value entirely and propose a different one at a higher proposal number. Both values then satisfy the local commit rule, and the register has two committed values for one slot. Nothing rejects the write at the time; the divergence surfaces later as replicas disagreeing on the contents of a log position.

### Implementation sketch (Scala)

The load-bearing part is the quorum predicate and the phase-1 recovery rule, not the messaging.

```scala
final case class Ballot(n: Int, node: String) extends Ordered[Ballot]:
  def compare(that: Ballot): Int =
    if n != that.n then n.compare(that.n) else node.compare(that.node)

final case class Acceptor(id: String, promised: Option[Ballot], accepted: Option[(Ballot, String)])

final class Config(val nodes: Set[String], val q1: Int, val q2: Int):
  require(q1 + q2 > nodes.size, "Q1 + Q2 must exceed N for cross-phase intersection")

/** Phase 1: the value the new leader is obliged to propose. */
def recover(replies: Set[Acceptor], cfg: Config): Either[String, Option[String]] =
  if replies.size < cfg.q1 then Left("phase 1 quorum not reached")
  else
    // The highest-ballot accepted value in Q1 dominates the leader's own proposal.
    Right(replies.flatMap(_.accepted).maxByOption(_._1).map(_._2))

/** Phase 2: commit once |Q2| acceptors have accepted at this ballot. */
def committed(acksAtBallot: Set[String], cfg: Config): Boolean =
  acksAtBallot.size >= cfg.q2

def promise(a: Acceptor, b: Ballot): Option[Acceptor] =
  if a.promised.exists(_ >= b) then None else Some(a.copy(promised = Some(b)))

def accept(a: Acceptor, b: Ballot, v: String): Option[Acceptor] =
  if a.promised.exists(_ > b) then None
  else Some(a.copy(promised = Some(b), accepted = Some((b, v))))
```

`recover` returning `Right(Some(v))` obliges the leader to propose `v`; `Right(None)` frees it to propose its own. The `require` in `Config` is the whole of the Flexible Paxos rule.

## Pitfalls

- **Shrinking Q2 without growing Q1.** Setting |Q1| + |Q2| ≤ N permits a new leader's phase-1 quorum to be disjoint from the committed phase-2 quorum; the leader misses the committed value and commits a different one at a higher ballot, producing two committed values for one slot.
- **Reusing a leader across a configuration change.** Quorum sizes changed while a leader is mid-term mean the old accepted values were committed under the old |Q2|, and the new |Q1| need not intersect those old quorums.
- **Assuming phase-2 quorums intersect.** Optimisations that let a leader answer reads from any Q2, or that treat a previously written quorum as authoritative, are correct under majorities and unsound under small Q2 — {a, b} and {c, d} are both valid quorums of a 5-node cluster with |Q2| = 2.
- **Skipping phase 1 for a leader that believes it is still leader.** The whole recovery of history sits in phase 1; a truncated or short-quorum prepare removes the only step that observes the previous leader's committed writes.
- **Reading the availability trade backwards.** A small Q2 raises write availability under slow or failed nodes but lowers availability of leader election, so a cluster can accept writes normally and then be unable to elect a successor after the leader fails.
