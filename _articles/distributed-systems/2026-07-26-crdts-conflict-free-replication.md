---
title: "CRDTs: merging without meeting — conflict-free replication for real"
date: 2026-07-26
track: distributed-systems
summary: "Quorums avoid conflicts by making writes overlap; vector clocks detect conflicts after the fact. CRDTs remove both problems by making merge mathematically incapable of disagreeing: the join-semilattice property, four concrete types, and why the merge function is the whole design."
reading_time: 6
tags: [crdt, replication, eventual-consistency, semilattice, distributed-data, van-steen]
sources:
  - title: "Shapiro, Preguiça, Baquero, Zawirski — A comprehensive study of Convergent and Commutative Replicated Data Types (INRIA RR-7506, 2011)"
    url: "https://inria.hal.science/inria-00555588/en/"
  - title: "Shapiro et al. — Conflict-free Replicated Data Types (SSS 2011)"
    url: "https://link.springer.com/chapter/10.1007/978-3-642-24550-3_29"
  - title: "crdt.tech — About CRDTs"
    url: "https://crdt.tech/"
  - title: "Riak KV docs — Concept: Data Types (CRDTs)"
    url: "https://docs.riak.com/riak/kv/latest/learn/concepts/crdts/index.html"
  - title: "Akka Distributed Data documentation"
    url: "https://doc.akka.io/libraries/akka-core/current/typed/distributed-data.html"
---

**Gist.** Replicas that accept writes independently will end up in divergent states, and the usual remedies either coordinate every write (quorums) or defer the conflict to an application-level reconciler (vector clocks). A conflict-free replicated data type (CRDT) removes the decision point by constraining the data type itself, so that combining any two replica states yields a correct result regardless of order, grouping or duplication. The cost is paid in the type system and in space: only operations expressible as a monotone lattice join are allowed, and the metadata that makes them monotone — per-replica slots, unique tags, tombstones — grows and must be reclaimed separately.

Quorum replication avoids conflicts by forcing read and write sets to overlap. Vector clocks detect conflicts after they have happened and surface two concurrent versions for manual reconciliation. CRDTs take a third path, formalised in Shapiro, Preguiça, Baquero and Zawirski's 2011 INRIA report, which established the term and grounded it in order theory.

## Two families, one property

Both families target **strong eventual consistency (SEC)**: once two replicas have delivered the same set of updates — in any order, with any duplicates — their states are identical. No consensus round, no leader, no blocking read or write.

- **CvRDT (state-based, "convergent")** — replicas exchange entire current states. Convergence follows from a `merge(x, y)` that computes the **least upper bound** of `x` and `y` in a join-semilattice. Reasoning is local, but whole states travel on the wire, so payload size and garbage collection dominate the engineering.
- **CmRDT (operation-based, "commutative")** — replicas broadcast individual operations ("increment", "add element e"). Convergence requires that **concurrent operations commute**, and typically a reliable causal-order broadcast channel: delivery must respect happens-before, the ordering vector clocks supply. Wire cost is lower; the delivery layer carries the burden.

Shapiro et al. show the two families are equivalent in expressive power — any op-based type has a state-based counterpart and conversely — so the choice between them is an engineering trade-off rather than a correctness one.

## Why merge must be a join-semilattice

For a CvRDT, `merge` is safe only if it is:

- **Commutative** — `merge(x, y) = merge(y, x)`. Replicas that receive peer states in different orders must still agree.
- **Associative** — `merge(merge(x, y), z) = merge(x, merge(y, z))`. Gossip fans out through arbitrary topologies; grouping cannot matter.
- **Idempotent** — `merge(x, x) = x`. Gossip retransmits, and the same state arriving twice must not be counted twice.

These three properties are the definition of a **join-semilattice**: a partial order in which every pair of elements has a unique least upper bound, with `merge` computing it. Payload state can therefore only move *up* the order, which is the formal content of "convergent". **A merge that violates any one of the three permits permanent divergence conditioned on message order** — the exact failure the type was introduced to eliminate.

## G-Counter and PN-Counter

A grow-only counter (G-Counter) holds one slot per replica; a replica increments only its own slot, the observed value is the sum of all slots, and merge takes the **pointwise maximum**. Maximum is commutative, associative and idempotent by construction: it is the join on the product order of per-replica counts. No slot ever decreases, so the state vector only ascends the lattice.

Decrement breaks that monotonicity directly, so a positive-negative counter (PN-Counter) is built from **two G-Counters**, one accumulating increments and one accumulating decrements, merged independently. The observable value is the difference of the two sums — a derived read, not a merged field. Each half remains grow-only, so the lattice argument is unchanged.

## LWW-Register and OR-Set

| Type | State | Merge rule | Failure mode |
|---|---|---|---|
| G-Counter | vector of per-replica counts | pointwise max | none (monotone by design) |
| PN-Counter | two G-Counters | merge each half | none |
| LWW-Register | (value, timestamp) | keep higher timestamp | concurrent writes silently drop one — a real write can vanish |
| OR-Set | set of (element, unique-tag) pairs, plus a tombstone set of removed tags | union adds, subtract observed-removed tags | none for causal ops, but the tag set grows without bound absent garbage collection |

**A last-writer-wins register (LWW-Register)** resolves every conflict by timestamp, a total order, so merge is trivially a join. The consequence is that **concurrent writes have a loser whose update is discarded without any record of the loss**. The type is conflict-free in the sense that it always terminates in agreement, not in the sense that it preserves intent.

**An observed-remove set (OR-Set)** most resembles vector-clock machinery. Every `add(e)` mints a fresh unique tag, producing a pair `(e, tag)`; `remove(e)` deletes only the tags for `e` that **this replica has already observed**, recording them in a tombstone set. That restriction is what makes concurrent add and remove commute: **a concurrent `add(e)` carries a tag the remover never observed, so it survives the merge** — add-wins semantics. The mechanism is structurally the same as a version vector: tag each event with its origin so that causality, not arrival order, decides the outcome.

### Implementation sketch (Scala)

```scala
final case class GCounter(counts: Map[String, Long]):
  def increment(id: String, n: Long = 1): GCounter =
    GCounter(counts.updated(id, counts.getOrElse(id, 0L) + n))

  def value: Long = counts.values.sum

  // join on the pointwise order: commutative, associative, idempotent
  def merge(other: GCounter): GCounter =
    GCounter((counts.keySet ++ other.counts.keySet).map { r =>
      r -> math.max(counts.getOrElse(r, 0L), other.counts.getOrElse(r, 0L))
    }.toMap)

final case class ORSet[A](live: Map[A, Set[String]], tombstones: Set[String]):
  def add(e: A, tag: String): ORSet[A] =
    copy(live = live.updated(e, live.getOrElse(e, Set.empty) + tag))

  // removes only tags this replica has already observed
  def remove(e: A): ORSet[A] =
    val seen = live.getOrElse(e, Set.empty)
    ORSet(live - e, tombstones ++ seen)

  def merge(other: ORSet[A]): ORSet[A] =
    val graves = tombstones ++ other.tombstones
    val union  = (live.keySet ++ other.live.keySet).flatMap { e =>
      val tags = live.getOrElse(e, Set.empty) ++ other.live.getOrElse(e, Set.empty)
      val kept = tags -- graves
      Option.when(kept.nonEmpty)(e -> kept)
    }.toMap
    ORSet(union, graves)

  def elements: Set[A] = live.keySet
```

## Where this ships

Riak's data types present an operation-based interface at the client application programming interface (API) and converge through state-based logic underneath (`riak_dt`), applying add-wins semantics for sets and maps and PN-Counter semantics for counters. Akka Distributed Data ships `GCounter`, `PNCounter`, `GSet`, `ORSet`, `ORMap` and `LWWRegister` as replicated data usable without a cluster leader. The workloads these target — multi-datacentre counters, shopping-cart sets — are ones where availability under partition is preferred to agreement on every write.

## Pitfalls

- **A merge function that is not idempotent double-counts under gossip retransmission.** Summing per-replica slots instead of taking their maximum inflates the G-Counter every time a peer state is redelivered.
- **An LWW-Register loses writes silently.** Two concurrent updates with distinct timestamps converge to the higher one, and the discarded value leaves no trace in the state, so the loss is invisible to monitoring and to the application.
- **LWW timestamps taken from unsynchronised wall clocks make the winner a function of clock skew,** not of write order: a replica whose clock runs ahead wins every conflict it participates in.
- **OR-Set tombstones grow monotonically.** Each `remove` retains the observed tags permanently, so a set with high add/remove churn accumulates metadata far exceeding the live payload unless a separate reclamation mechanism runs.
- **Reusing a tag across `add` calls breaks add-wins.** A tag already in a tombstone set causes a later, causally unrelated add to be filtered out on merge, so the element disappears without any remove having been issued.
- **CmRDTs deployed over a channel that does not guarantee causal-order delivery diverge.** Commutativity is required only of *concurrent* operations; operations that are causally related but delivered out of order violate the precondition and the convergence proof no longer applies.
- **Non-monotone operations cannot be retrofitted.** Adding a decrement path to a G-Counter's single slot breaks the lattice property, which is why the PN-Counter carries two counters rather than allowing slots to fall.
