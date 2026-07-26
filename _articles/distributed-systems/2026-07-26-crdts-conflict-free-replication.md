---
title: "CRDTs: merging without meeting — conflict-free replication for real"
date: 2026-07-26
track: distributed-systems
summary: "Quorums avoid conflicts by making writes overlap; vector clocks detect conflicts after the fact. CRDTs skip both problems by making merge mathematically incapable of disagreeing. Here's the join-semilattice trick, four concrete types, and why the merge function is the whole design."
reading_time: 5
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

Quorum replication (see the previous article) avoids conflicts by forcing read and write sets to overlap. Vector clocks detect conflicts once they've already happened, and hand you two concurrent versions to reconcile by hand. CRDTs take a third path: design the data type so that *any* two replica states can be combined into a correct result, no coordination and no human tie-breaker required. This is the core idea behind Shapiro et al.'s 2011 INRIA report, which coined the term and gave it a rigorous foundation in order theory.

## Two families, one property

The property both families aim for is **strong eventual consistency (SEC)**: once two replicas have seen the same set of updates — in any order, with any duplicates — they hold identical state. No consensus round, no leader, no blocking.

- **CvRDT (state-based, "Convergent")** — replicas exchange their entire current state. Convergence comes from a `merge(x, y)` function that computes the *least upper bound* of `x` and `y` in a join-semilattice. Simple to reason about (merge is just "combine and take the max"), but you ship whole states, so garbage collection and payload size matter.
- **CmRDT (operation-based, "Commutative")** — replicas broadcast individual operations (e.g. "increment", "add element e"). Convergence requires that *concurrent* operations commute with each other, and typically needs a reliable causal-order broadcast channel (delivery must respect happens-before, which is exactly the ordering vector clocks give you). Cheaper on the wire, but the delivery layer has to do more work.

Shapiro et al. prove the two are equivalent in expressive power — anything you can build op-based you can build state-based and vice versa — so the choice is an engineering trade-off, not a correctness one.

## Why merge must be a join-semilattice

For CvRDT, `merge` is only safe if it is:

- **Commutative** — `merge(x, y) = merge(y, x)`. Replicas that see updates from different peers first must still agree.
- **Associative** — `merge(merge(x, y), z) = merge(x, merge(y, z))`. Gossip fans out through arbitrary topologies; grouping can't matter.
- **Idempotent** — `merge(x, x) = x`. Gossip protocols retransmit; the same state arriving twice must not double-count.

Together these three properties are exactly the definition of a **join-semilattice**: a partial order where every pair of elements has a unique least upper bound, and `merge` computes that upper bound. Payload state can only move *up* the lattice — this is what "convergent" formally means. If your merge function violates any one property, replicas can permanently disagree depending on message order, which defeats the entire point.

## G-Counter: the simplest CvRDT

A grow-only counter keeps one slot per replica and sums them:

```python
class GCounter:
    def __init__(self, replica_id, replica_ids):
        self.id = replica_id
        self.counts = {r: 0 for r in replica_ids}

    def increment(self, n=1):
        self.counts[self.id] += n

    def value(self):
        return sum(self.counts.values())

    def merge(self, other):
        merged = {r: max(self.counts[r], other.counts[r])
                  for r in self.counts}
        self.counts = merged
        return self
```

`max` per slot is commutative, associative, and idempotent by construction — it's the join on the semilattice of per-replica counts ordered pointwise. No replica's slot ever decreases, so the vector only moves up.

## PN-Counter: adding decrements

A single counter can't support decrement without breaking monotonicity, so you run two G-Counters and subtract:

```python
class PNCounter:
    def __init__(self, replica_id, replica_ids):
        self.p = GCounter(replica_id, replica_ids)  # increments
        self.n = GCounter(replica_id, replica_ids)  # decrements

    def increment(self, k=1): self.p.increment(k)
    def decrement(self, k=1): self.n.increment(k)
    def value(self):          return self.p.value() - self.n.value()
    def merge(self, other):
        self.p.merge(other.p)
        self.n.merge(other.n)
        return self
```

Each half is still grow-only and mergeable; the observable value is a derived read, not a merged field.

## LWW-Register and OR-Set

| Type | State | Merge rule | Failure mode |
|---|---|---|---|
| G-Counter | vector of per-replica counts | pointwise max | none (monotone by design) |
| PN-Counter | two G-Counters | merge each half | none |
| LWW-Register | (value, timestamp) | keep higher timestamp | concurrent writes silently drop one — a real write can vanish |
| OR-Set | set of (element, unique-tag) pairs, plus a tombstone set of removed tags | union adds, subtract observed-removed tags | none for causal ops, but tag set grows unboundedly without GC |

**LWW-Register** resolves every conflict by timestamp, which is trivially a total order — easy, but it means concurrent writes have a *loser* whose update is silently discarded. It's only "conflict-free" in the sense that it always terminates in agreement, not that it preserves intent.

**OR-Set** (observed-remove set) is the type that most resembles vector-clock machinery: every `add(e)` mints a fresh unique tag `(e, tag)`, and `remove(e)` only removes the tags for `e` that *this replica has already observed*, storing them in a tombstone set. That "only remove what you've seen" rule is what makes concurrent add/remove commute correctly — a concurrent `add(e)` carries a tag the remover never observed, so it survives the merge (add-wins semantics):

```js
function mergeORSet(a, b) {
  const elements = new Map(); // element -> Set of live tags
  for (const [e, tags] of [...a.elements, ...b.elements]) {
    const live = new Set([...tags].filter(
      t => !a.tombstones.has(t) && !b.tombstones.has(t)));
    if (live.size) elements.set(e, live);
  }
  return { elements, tombstones: new Set([...a.tombstones, ...b.tombstones]) };
}
```

This is structurally the same trick as a version vector: tag every event with its origin so causality, not arrival order, decides the outcome.

## Where this actually ships

Riak's data types are operation-based on the client API but converge via state-based logic underneath (`riak_dt`), applying add-wins for sets and maps and PN-Counter semantics for counters. Akka Distributed Data ships `GCounter`, `PNCounter`, `GSet`, `ORSet`, `ORMap`, and `LWWRegister` directly as replicated data usable without a cluster leader. Both exist because coordinating every write through Raft or a quorum is sometimes the wrong trade — multi-datacenter counters and shopping-cart sets are exactly the workloads where "always available, eventually correct" beats "sometimes unavailable, always agreed."

**Try next:** implement the OR-Set above with three simulated replicas, feed them `add("x")`, `remove("x")`, and a concurrent `add("x")` in random delivery orders, and confirm every ordering converges to the same live set — then swap in a plain LWW-Register for the same scenario and watch a write silently disappear.
