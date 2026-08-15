---
title: 'CAP and PACELC: the trade-off is C-vs-A only during a partition'
date: 2026-08-10
track: distributed-systems
summary: CAP is not 'pick 2 of 3.' Partitions are imposed by the network rather than chosen, so the only decision CAP forces is consistency versus availability, and only while a partition holds. PACELC adds the case CAP omits — the latency-versus-consistency trade-off paid during normal operation. This article gives the precise definitions, Brewer's own correction, classifications of real systems, and a mechanism-first procedure for deciding whether a system is CP or AP.
reading_time: 7
tags:
- cap-theorem
- pacelc
- consistency
- availability
- linearizability
- abadi
- latency
sources:
- title: Gilbert & Lynch, Brewer's Conjecture and the Feasibility of Consistent, Available, Partition-Tolerant Web Services (SIGACT News 2002)
  url: https://www.comp.nus.edu.sg/~gilbert/pubs/BrewersConjecture-SigAct.pdf
- title: 'Brewer, CAP Twelve Years Later: How the ''Rules'' Have Changed (IEEE Computer, 2012)'
  url: https://ieeexplore.ieee.org/document/6133253/
- title: Abadi, Consistency Tradeoffs in Modern Distributed Database System Design (IEEE Computer, 2012)
  url: https://www.cs.umd.edu/~abadi/papers/abadi-pacelc.pdf
- title: Abadi, Problems with CAP, and Yahoo's little known NoSQL system (DBMS Musings, 2010)
  url: https://dbmsmusings.blogspot.com/2010/04/problems-with-cap-and-yahoos-little.html
- title: PACELC design principle — Wikipedia
  url: https://en.wikipedia.org/wiki/PACELC_design_principle
- title: Gilbert & Lynch, Brewer's Conjecture and the Feasibility of Consistent, Available, Partition-Tolerant Web Services (ACM SIGACT News 33:2, 2002)
  url: https://www.cs.princeton.edu/courses/archive/spr22/cos418/papers/cap.pdf
- title: 'Brewer, CAP Twelve Years Later: How the "Rules" Have Changed (IEEE Computer, Feb 2012)'
  url: https://sites.cs.ucsb.edu/~rich/class/cs293b-cloud/papers/brewer-cap.pdf
---

**Gist.** A replicated store cannot answer every request and still present a single total order of operations while the network is split. The consistency–availability–partition-tolerance (CAP) theorem formalises that impossibility, and the resolution is a per-system policy: during a partition, either refuse requests the system cannot order, or answer them and accept divergence. PACELC extends the statement to the partition-free case, where the cost of a single order is not unavailability but the round-trip latency of coordination.

## The three properties, defined precisely

Gilbert and Lynch's 2002 SIGACT News paper gives CAP its only rigorous form. The letters denote specific guarantees, not vague qualities:

- **Consistency (C)** means **linearizability**, also called atomic consistency: a single total order of operations consistent with real time, such that a read returns the value of the most recent completed write, as though the system were one register. This is not the "C" of atomicity–consistency–isolation–durability (ACID), which denotes integrity constraints. Same letter, different concept.
- **Availability (A)** means **every request received by a non-failing node must result in a response** — the algorithm running on that node has to terminate. A node that is running but declines to answer because it cannot reach its peers is, under this definition, unavailable.
- **Partition tolerance (P)** means **the network may drop or delay arbitrarily many messages** between nodes and the system continues to operate. A partition is a split in which two groups of nodes cannot exchange messages.

The theorem states that no distributed system guarantees all three simultaneously. Split the nodes into groups `{G1}` and `{G2}` with no communication between them. Write `v2` to `G1`, then read from `G2`. If the read returns any response at all (A), it can only return the stale `v1`, because no message crossed the partition (P), so the execution is not linearizable (C fails). If C is instead preserved, `G2` must block or error on the read (A fails). **The sacrifice is confined to the interval during which the partition holds** — that conditional clause carries the entire result.

## The misconception: "pick 2 of 3"

**Partition tolerance is not a property a designer elects to drop.** Partitions arise from the network — dropped packets, a failed switch, a garbage-collection pause that makes a live node indistinguishable from a dead one — and the system has no vote in the matter. Over any real network, partitions occur. "CA", meaning the abandonment of P in order to retain C and A, therefore does not describe a distributed design point; it describes a single-node database, or a cluster that halts entirely when the network breaks.

The genuine choice is binary and conditional: **when a partition occurs, is C or A sacrificed?** That is the only decision CAP forces, and it applies solely for the duration of the partition. Hence "CP" and "AP" are the meaningful labels, and "CA" is a category error.

## Brewer's 2012 correction

Brewer's *CAP Twelve Years Later* (IEEE Computer, February 2012) revises the slogan he popularised. Its clarifications:

1. **"Because partitions are rare, there is little reason to forfeit C or A when the system is not partitioned."** Absent a partition, both strong consistency and full availability are attainable. CAP constrains nothing about normal operation.
2. **The 2-of-3 framing misleads** by treating the three properties as symmetric and continuously in force. C and A are traded against each other only during a partition.
3. **C, A and P are more continuous than binary.** Consistency and availability admit degrees, and the level chosen can differ between subsystems or between operations: one endpoint may be strongly consistent while another is eventually consistent.
4. Design is reframed around the **partition lifecycle**: *detect* the partition, *enter an explicit partition mode* that restricts some operations, then *recover* — reconcile state and compensate for operations performed in error — once communication resumes.

The modern reading of CAP is correspondingly narrow: a statement about behaviour *during* partitions, and nothing further.

## PACELC: the case CAP omits

CAP is silent about the common case in which the network is healthy. Fully replicated systems nonetheless trade off continuously: to answer with the strongest consistency, a node must coordinate with other replicas, and coordination costs **latency**. Abadi's PACELC — stated in a 2010 DBMS Musings post and developed in a 2012 IEEE Computer paper — covers both cases:

> **if Partition (P) then choose Availability (A) or Consistency (C); Else (E) choose Latency (L) or Consistency (C).**

The `PAC` half restates CAP. The `ELC` half is the always-applicable addition: with no partition, a low-latency design must weaken consistency (fewer replicas on the critical path, asynchronous replication), and a strongly consistent design must pay round trips (synchronous quorums, leader coordination). Abadi's "consistency" in PACELC denotes strong, linearizability-style consistency generally rather than only the atomic register of the Gilbert–Lynch proof.

## Classifying real systems

Each system receives two letters: what it forfeits under partition, and what it forfeits otherwise.

| System | PACELC | Under partition | Normal operation | Mechanism |
|---|---|---|---|---|
| Dynamo-style / Cassandra / Riak | **PA/EL** | keep Availability | keep Latency | Leaderless replication with per-request replica counts; tunable, but the low settings answer without waiting for agreement |
| HBase / BigTable | **PC/EC** | keep Consistency | keep Consistency | Single master per region or tablet; a partitioned region becomes unavailable rather than divergent |
| Spanner | **PC/EC** | keep Consistency | keep Consistency | Paxos plus TrueTime give external consistency; commit-wait adds latency that is not traded away |
| MongoDB | **PA/EC** | keep Availability | keep Consistency | A primary cut into the minority side keeps accepting writes until it steps down, so those writes may be rolled back; in the healthy case reads go to the primary |
| PNUTS (Yahoo) | **PC/EL** | keep Consistency | keep Latency | Per-record timeline consistency with a master; reads are served from a local, possibly stale replica. This is the case that motivated PACELC |
| Single-node RDBMS (MySQL) | — | not applicable | not applicable | Unreplicated, so neither half of PACELC has anything to trade; this is the shape the "CA" label describes |

The `PA/EL` versus `PC/EC` split follows the leaderless-versus-leadered architectural fork. The informative entries are the asymmetric ones — **PA/EC** (MongoDB) and **PC/EL** (PNUTS) — which the two-letter CAP vocabulary cannot express, and which are the reason PACELC exists.

Position on the C/L axis is frequently a quorum setting; see [quorum replication and why R + W > N is the whole game](/articles/distributed-systems/2026-07-25-quorum-replication-r-plus-w). Cassandra's tunable consistency selects `R` and `W` per request, sliding a single cluster along the `EL`↔`EC` spectrum.

## Deriving the label from the mechanism

The label is a conclusion, not a premise. The derivation proceeds in three steps:

1. **Locate the coordination point.** A write becomes durable and visible at a single leader, at a quorum, or at any replica. That location determines the behaviour of a node cut off from the rest.
2. **Determine what a partitioned node does with a request.** It either answers, yielding **AP** with the risk of staleness, or errors or blocks until it can reach a quorum, yielding **CP** at the cost of availability. **That single behaviour is the CAP classification.**
3. **Add the PACELC half.** In the healthy case the system either coordinates before answering (**EC**, higher latency) or answers locally and reconciles later (**EL**).

Real systems are **tunable and per-operation**: Cassandra at `QUORUM`/`QUORUM` behaves far more like CP than the same cluster at `ONE`/`ONE`. An accurate classification names both the default and the knob.

## A worked example: reading PACELC off a quorum configuration

For tunable stores, the PACELC label is a configuration decision rather than a product property. Cassandra with `N=3` replicas:

```text
replication factor N = 3

EC end: read and write sets intersect (R + W > N)
  write consistency level QUORUM   W = 2
  read  consistency level QUORUM   R = 2   ->  R + W = 4 > 3, overlap guaranteed

EL end: same cluster, favour latency, accept staleness
  write consistency level ONE      W = 1
  read  consistency level ONE      R = 1   ->  R + W = 2 < 3, may read stale
```

The consistency level is chosen per request rather than fixed in the cluster's configuration file. The first setting yields `EC`: every read set intersects the set that acknowledged the last write, so at least one responding replica holds it, at the cost of waiting for a second node. The second yields `EL`: single-node round trips, lowest latency, no overlap guarantee. The same setting governs behaviour under partition. With `QUORUM` writes, the minority side — one node of three — cannot reach `W=2` and **refuses writes**, which is `PC`. With `ONE`, the isolated node continues accepting writes, which is `PA`. One knob moves the system across both halves of PACELC.

### Implementation sketch (Scala)

The quorum rule is the mechanism behind both halves. The sketch below collects replica acknowledgements and returns as soon as `w` of them arrive; a partitioned minority never reaches `w` and the future completes exceptionally, which is the `PC` behaviour, whereas `w = 1` completes on the local replica alone, which is `PA`.

```scala
final case class Ack(replica: Int)

/** Completes once `w` replicas acknowledge, or fails once fewer than `w`
  * can still succeed — the point at which a partitioned minority gives up. */
def awaitQuorum(acks: Seq[Future[Ack]], w: Int)(using
    ExecutionContext): Future[Seq[Ack]] =
  val promise = Promise[Seq[Ack]]()
  val done    = new AtomicReference(Vector.empty[Ack])
  val pending = new AtomicInteger(acks.size)

  acks.foreach { f =>
    f.onComplete { outcome =>
      outcome match
        case Success(a) =>
          val soFar = done.updateAndGet(_ :+ a)
          if soFar.size >= w then promise.trySuccess(soFar)
        case Failure(_) => ()
      // remaining = successes still possible; below w the quorum is unreachable
      if pending.decrementAndGet() + done.get.size < w then
        promise.tryFailure(new IllegalStateException("quorum unreachable"))
    }
  }
  promise.future

// R + W > N is the set-overlap condition, not a linearizability guarantee:
// with N = 3, (w = 2, r = 2) intersect; (w = 1, r = 1) do not.
def overlaps(r: Int, w: Int, n: Int): Boolean = r + w > n
```

## Pitfalls

- **Labelling a system "CA".** The label describes a cluster that stops when the network breaks, not one that tolerates partitions; applying it to a replicated store hides the fact that no partition policy has been chosen.
- **Reading CAP's "C" as ACID's "C".** CAP consistency is linearizability, an ordering property; ACID consistency is constraint preservation. A store may satisfy either without the other.
- **Treating a garbage-collection pause as distinct from a partition.** From a peer's perspective a paused node and an unreachable node are indistinguishable, so a long pause triggers the same partition-mode behaviour — including failover and the divergence that follows.
- **Quoting a product-level PACELC label for a tunable store.** Cassandra at `ONE`/`ONE` and at `QUORUM`/`QUORUM` sit at opposite ends of both halves; the label without the consistency level states nothing.
- **Assuming `R + W > N` yields linearizability.** The inequality guarantees that read and write sets intersect; it does not by itself order concurrent writes, so last-write-wins reconciliation can still discard an acknowledged update.
- **Omitting the recovery phase.** Brewer's lifecycle ends in reconciliation and compensation; a system entering partition mode without a defined merge path resumes with divergent state and no procedure for resolving it.
