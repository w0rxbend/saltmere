---
title: "Vector clocks in ~40 lines — what they add over Lamport clocks"
date: 2026-07-24
track: distributed-systems
summary: "Lamport clocks report that events might be causally related; vector clocks decide whether they are. The smallest implementation that separates the two."
reading_time: 6
tags: [causality, logical-clocks, coordination, van-steen]
sources:
  - title: "van Steen & Tanenbaum, Distributed Systems (4th ed.), §5.2 Logical clocks"
    url: "https://www.distributed-systems.net/index.php/books/ds4/"
  - title: "Lamport, Time, Clocks, and the Ordering of Events in a Distributed System (1978)"
    url: "https://lamport.azurewebsites.net/pubs/time-clocks.pdf"
---

**Gist.** A distributed system has no global clock, so "when did this event happen?" has no answer; the answerable question is whether event A *happened-before* event B — whether A could have influenced B. Lamport clocks assign one integer per process and guarantee only the forward implication (A → B implies `L(A) < L(B)`); vector clocks assign one counter per process and make the implication an equivalence, so concurrency becomes detectable. The cost is space and metadata: every stamp is `O(N)` in the number of processes, and every message carries the whole vector.

## Lamport clocks: necessary but not sufficient

A Lamport clock is a single integer `L` held by each process. Lamport's 1978 paper states two implementation rules — IR1 for local events, IR2 for messages — which in a message-passing system read as three steps:

1. Increment `L` before each local event.
2. Attach the current `L` to every outgoing message.
3. On receipt of a message stamped `t`, set `L = max(L, t) + 1`.

These rules establish the invariant **A → B implies `L(A) < L(B)`**, where → is the happened-before relation: the transitive closure of "same process, earlier" and "send precedes matching receive".

The **converse fails**. `L(A) < L(B)` does not imply A → B. Two processes that never exchange a message still advance their counters independently, so concurrent events routinely receive different integers, and nothing in the pair of integers distinguishes that case from genuine causal dependence. A single integer collapses a partial order into a total order, and the collapse is lossy in exactly one direction: it preserves every real dependency and invents dependencies that do not exist.

The consequence is a limit on what Lamport clocks can be used for. They are sufficient to build *a* consistent total order — for example, to serialise requests in a distributed mutual-exclusion protocol, where any consistent order is acceptable. They are insufficient for conflict detection, where the question is whether two replica writes to the same key were causally ordered (the later one supersedes) or concurrent (both must be retained or reconciled).

## Vector clocks: capturing causality exactly

Each of `N` processes holds a vector `V` of `N` counters, indexed by process. The update rules mirror Lamport's:

1. Process `i` increments `V[i]` on a local event.
2. Process `i` increments `V[i]` and sends the **entire vector** with the message.
3. On receipt of vector `W`, process `i` sets `V[k] = max(V[k], W[k])` for every `k`, then increments `V[i]`.

The element-wise maximum is the load-bearing step: it imports the sender's knowledge of every other process's progress, so `V[k]` means **"the number of events at process `k` that this process knows about"**. Comparison then reads off causality directly:

- `V(A) < V(B)` — every slot of `V(A)` is ≤ the corresponding slot of `V(B)`, and at least one is strictly less — **iff** A → B.
- If neither `V(A) < V(B)` nor `V(B) < V(A)`, the events are **concurrent**: neither could have influenced the other.

Vector comparison is a partial order, not a total one, and that is precisely why it is faithful: the happened-before relation is itself a partial order, and any total order must therefore lose information.

A concrete trace makes the difference visible. Take three processes P0, P1, P2, all starting at `[0,0,0]`. P0 performs a local event and sends to P1, stamping `[2,0,0]` (one increment for the local event, one for the send). Independently, before that message chain reaches it, P2 performs a local event and reaches `[0,0,1]`. Comparing `[2,0,0]` with `[0,0,1]`: slot 0 is greater in the first, slot 2 is greater in the second, so neither dominates and the pair is correctly reported concurrent. Two Lamport integers drawn from the same trace — say 2 and 1 — would order the events, and the ordering would be an artefact.

### Implementation sketch (Scala)

```scala
final case class VectorClock(index: Int, counters: Vector[Long]):

  /** A local event: only this process's own slot advances. */
  def local: VectorClock =
    copy(counters = counters.updated(index, counters(index) + 1))

  /** Stamp attached to an outgoing message. */
  def send: (VectorClock, Vector[Long]) =
    val next = local
    (next, next.counters)

  /** Merge an incoming stamp: element-wise max, then own increment. */
  def receive(stamp: Vector[Long]): VectorClock =
    val merged = counters.lazyZip(stamp).map(_ max _)
    VectorClock(index, merged).local

object VectorClock:

  def happensBefore(a: Vector[Long], b: Vector[Long]): Boolean =
    a.lazyZip(b).forall(_ <= _) && a.lazyZip(b).exists(_ < _)

  def concurrent(a: Vector[Long], b: Vector[Long]): Boolean =
    !happensBefore(a, b) && !happensBefore(b, a)

  /** Conflict detection: siblings are the writes no other write dominates. */
  def siblings[V](versions: List[(Vector[Long], V)]): List[(Vector[Long], V)] =
    versions.filterNot: (stamp, _) =>
      versions.exists: (other, _) =>
        happensBefore(stamp, other)
```

`siblings` is the whole of replica conflict detection expressed in the comparison: a stored version survives if no other stored version strictly dominates it. When exactly one survives, the write order was unambiguous; when several survive, the writes were concurrent and the application — or a merge function — must decide.

## Cost and the retreat from full vectors

Each stamp is `O(N)` in space and each comparison `O(N)` in time, where **`N` counts writers, not requests**. That distinction determines where vector clocks are affordable. A handful of replicas producing versions keeps vectors short. A system in which every client is an independent writer makes `N` grow without bound, and the metadata attached to a value can exceed the value.

Systems that hit the bound retreat from full vectors in one of two directions. One is to truncate: cap the number of entries a stamp may carry and drop the least recently updated, which keeps the stamp bounded at the price of reporting some ordered pairs as concurrent. The other is to change what an entry counts — attaching entries to a small fixed set of replicas rather than to clients, so that `N` is bounded by the replication factor. A third clock family, the hybrid logical clock (HLC), pairs a physical timestamp with a Lamport-style counter in constant space, but recovers only the forward implication: an HLC comparison cannot certify concurrency the way a vector comparison can. Each option trades some exactness of the causality test for a bounded stamp; which exactness to give up is the design decision.

## Pitfalls

- **Reading `L(A) < L(B)` as causality.** Lamport integers order every pair of events, including unrelated ones, so a comparison that "succeeds" carries no information about influence; only the forward direction A → B ⟹ `L(A) < L(B)` holds.
- **Omitting the own-slot increment on receive.** Merging by element-wise maximum alone leaves the receiving process's stamp equal to the sender's, so the receive does not compare as strictly after the send and the two events read as identical rather than ordered.
- **Growing the vector by process identity rather than writer identity.** Allocating a slot per client, per session, or per request makes `N` unbounded, and the stamp outgrows the data it annotates.
- **Reusing a process index after a restart with fresh state.** A process that resumes at zero in a slot others have already advanced past will have its subsequent events dominated by stale entries, and its writes silently discarded as superseded.
- **Comparing vectors of different lengths or with mismatched index assignments.** Zipping truncates to the shorter vector, so a missing slot is read as agreement and unrelated writes compare as ordered.
- **Assuming concurrency detection resolves the conflict.** Vector clocks report that two versions are concurrent; they supply no merge, so a store without an application-level reconciliation rule accumulates siblings indefinitely.
