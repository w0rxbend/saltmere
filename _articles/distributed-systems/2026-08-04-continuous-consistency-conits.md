---
title: "Conits and continuous consistency: three dials between strong and eventual"
date: 2026-08-04
track: distributed-systems
summary: "Strong and eventual consistency are two endpoints of a continuum. Yu and Vahdat's conit model measures the space between them along three independent axes — numerical, ordering, and staleness deviation — and lets an application bound each one. This article covers what the three dimensions mean, the textbook two-replica example, a sketch of numerical-bound enforcement, and how Azure Cosmos DB exposes the same idea as a configuration knob."
reading_time: 6
tags: [consistency, replication, conits, bounded-staleness, distributed-systems]
sources:
  - title: "Van Steen & Tanenbaum — Distributed Systems (4th ed.), Chapter 7: Consistency and Replication"
    url: "https://www.distributed-systems.net/index.php/books/ds4/"
  - title: "Yu & Vahdat — Design and Evaluation of a Conit-Based Continuous Consistency Model for Replicated Services (ACM TOCS 20(3), 2002)"
    url: "https://dl.acm.org/doi/10.1145/566340.566342"
  - title: "Yu & Vahdat — Design and Evaluation of a Continuous Consistency Model for Replicated Services (OSDI 2000, free PDF)"
    url: "https://www.usenix.org/legacy/events/osdi2000/full_papers/yuvahdat/yuvahdat.pdf"
  - title: "TACT — Tunable Availability and Consistency Tradeoffs (Duke ISSG project page)"
    url: "https://www2.cs.duke.edu/ari/issg/TACT/"
  - title: "Azure Cosmos DB — Consistency levels (bounded staleness)"
    url: "https://learn.microsoft.com/en-us/azure/cosmos-db/consistency-levels"
---

**Gist.** A named consistency ladder forces an application to pick a rung, even when its tolerance for divergence differs by dimension. Yu and Vahdat's **continuous consistency** model, implemented in the TACT (Tunable Availability and Consistency Tradeoffs) toolkit, instead quantifies current divergence along three independent axes — numerical, ordering, and staleness deviation — and lets the application cap each one; a replica that would breach a cap must propagate writes rather than continue serving from local state. The cost is that every bound converts into forced anti-entropy traffic and synchronous waiting exactly when the workload is most active.

Strong consistency is the point where all caps are zero; eventual consistency is the point where all caps are unbounded. The useful configurations lie between.

This is a **data-centric** model. The [previous article](../2026-07-30-client-centric-consistency-session-guarantees/) covered *client-centric* session guarantees, which constrain what a single session may observe and say nothing about how replicas relate to one another. Continuous consistency bounds how far the *replicas themselves* may diverge from the ideal final state, for all observers simultaneously, using numeric limits on global drift rather than per-session bookkeeping.

## Three independent dimensions of inconsistency

The model's central claim is that the divergence of a replica is not one number but three, and that the three vary independently. Van Steen and Tanenbaum present them as the axes along which a conit's bounds are enforced:

| Dimension | Question it answers | Bounded by |
|---|---|---|
| **Numerical deviation** | How far is this value from the fully-converged value? | Number *and* total weight of unseen writes |
| **Ordering deviation** | How many applied writes are still tentative and could be reordered? | Count of outstanding (uncommitted) writes at the replica |
| **Staleness deviation** | How old is the oldest missing write? | Wall-clock time since that write was accepted elsewhere |

**Numerical deviation** captures value drift. If a conit holds a stock count and other replicas have accepted sales this replica has not yet received, the count is numerically stale by the summed **weight** of those missing writes, where weight is application-defined and is commonly the magnitude of the change. The dimension has an absolute form (total unseen weight) and a relative form (a percentage of the true value).

**Ordering deviation** concerns *tentative* writes. In an optimistic replica, a locally-accepted write is provisional: it may be reordered or rolled back once the global commit order is known. Ordering deviation is the count of such outstanding writes. **A bound of zero forces every write to commit before it becomes visible**; a loose bound permits many speculative writes that may later require reordering.

**Staleness deviation** is the time axis: the interval between now and the acceptance time, elsewhere, of the oldest write this replica has not yet seen. A 10-second staleness bound states that no write may remain invisible for longer than 10 seconds, independent of how many writes or how much weight are involved.

## Conits: the unit the bounds apply to

A **conit** — consistency unit — is the granularity over which the three bounds are enforced. It is an application-defined logical or physical unit: a single record, a group of related fields, an entire table. Granularity is a genuine trade. **A coarse conit is cheap to track but causes false sharing**: an unrelated write anywhere inside the conit counts against every bound defined on it. A fine conit is precise but multiplies vector-clock state and anti-entropy overhead. The sizing problem resembles lock sizing.

## The textbook two-replica example

The canonical figure has a conit containing two variables, `x` and `y`, replicated at A and B. Consider replica A. It has applied four operations to the conit: three are its own, still-tentative writes (`y += 2`, `y += 5`, `x += 4`), and one is a write received from B and already made permanent (`x += 2`, tagged `<5,B>`). A summarises what it has seen with a vector clock `(15, 5)`: 15 of its own operations, 5 of B's.

The deviations follow directly from that state:

- **Ordering deviation = 3.** A holds three tentative writes of its own that are not yet globally committed and remain reorderable.
- **Numerical deviation = (1, 5).** From clocks exchanged during anti-entropy, A knows B has accepted **1** write A has not seen, of **weight 5**. A's value of the conit may therefore be wrong by up to 5 units.

B maintains the symmetric bookkeeping about A. **When a replica's tracked deviation would exceed its configured bound, the replica must push or pull writes until the bound is restored.** That forced propagation is the entire enforcement mechanism; nothing else prevents drift.

## Enforcing a numerical bound

Numerical deviation can be enforced with purely local bookkeeping because weight is additive. TACT splits the *global* numerical-error budget across the N replicas, so **each replica is individually responsible for keeping the weight it has accepted but not yet pushed below its own share**. If every replica honours its slice, the total is bounded by construction, with no global coordination on the common path.

### Implementation sketch (Scala)

```scala
final case class Write(weight: Double, payload: String)

final class ConitReplica(id: Int, peers: Set[Int], globalBound: Double):
  // this replica's share of the global error budget
  private val localBound: Double = globalBound / (peers.size + 1)

  // weight accepted here but not yet propagated, tracked per peer
  private var unpropagated: Map[Int, Double] = peers.map(_ -> 0.0).toMap
  private var log: Vector[Write] = Vector.empty

  def acceptWrite(w: Write): Unit =
    apply(w)
    log = log :+ w
    unpropagated = unpropagated.map((p, d) => p -> (d + math.abs(w.weight)))
    enforceNumericalBound()

  private def enforceNumericalBound(): Unit =
    unpropagated.foreach: (peer, drift) =>
      // the peer's view of this replica would breach its share of the budget
      if drift >= localBound then pushTo(peer)

  private def pushTo(peer: Int): Unit =
    send(peer, logSince(peer))
    unpropagated = unpropagated.updated(peer, 0.0)

  // ... unchanged: state update, log slicing by peer cursor, transport
  private def apply(w: Write): Unit = ()
  private def logSince(peer: Int): Vector[Write] = log
  private def send(peer: Int, ws: Vector[Write]): Unit = ()
```

The shape generalises to the other two axes. For **staleness**, the weight accumulator becomes a per-peer timestamp of the oldest unpushed write, and propagation triggers as `now - oldestUnpushedAcceptTime` approaches the bound. For **ordering**, reaching the outstanding-write limit triggers the write-commitment protocol: agree on a global order, then make tentative writes permanent.

## The trade each dial encodes

Each bound trades consistency against availability and performance. **Tight bounds force frequent eager propagation**: more messages, more synchronous waiting, and less tolerance for a slow or partitioned peer, in exchange for replica views that stay close to converged. **Loose bounds let a replica serve reads and absorb writes from local state alone** until a bound trips. The three dials are independent, so an inventory service may set a tight numerical bound while tolerating seconds of staleness and dozens of tentative writes.

## The same idea in a shipped system

Azure Cosmos DB's **bounded staleness** level exposes the staleness and ordering axes as a configuration knob. Two bounds are configured — **K**, a number of versions (writes), and **T**, a time interval — and the store guarantees that reads lag the latest write by at most K versions *or* T seconds, whichever is reached first. Those are the ordering-count and time-staleness dimensions under product names. The permitted floors differ by account topology: **single-region accounts allow a minimum of K = 10 writes or T = 5 seconds, while multi-region accounts require at least K = 100,000 writes or T = 300 seconds**. Bounded staleness sits between Strong and Session among Cosmos DB's five levels, and leaves the numerical axis to the application.

## Pitfalls

- **A conit sized to cover an entire dataset makes unrelated writes trip the bound.** False sharing is the symptom: a bound configured for one hot counter forces propagation whenever any field in the conit changes.
- **Splitting the global numerical budget across N replicas assumes N is stable.** Adding a replica without recomputing `localBound` leaves the sum of the per-replica slices above the intended global bound.
- **A zero ordering bound removes the benefit of optimistic acceptance.** Every write must complete the commitment protocol before becoming visible, so write latency becomes the cost of global ordering.
- **A staleness bound is enforced against wall-clock acceptance times at other replicas.** Clock skew between replicas therefore shifts the effective bound in both directions.
- **Weight is application-defined, so a write whose weight is reported as zero never contributes to numerical deviation.** A replica can then drift arbitrarily in value while every numerical bound reports compliance.
- **In Cosmos DB, a bounded-staleness account cannot be configured below the documented floors.** Requesting K = 10 on a multi-region account is rejected, so a design assuming single-region floors fails on the first region addition.
