---
title: "Sequential vs causal consistency: which histories are legal, and why geo-replication settles for causal"
date: 2026-08-04
track: distributed-systems
summary: "Data-centric consistency models are contracts over the set of legal execution histories. Sequential consistency (Lamport, 1979) demands one global interleaving; causal consistency only orders writes that are causally related. This article decides concrete histories under each model, locates where they diverge, and explains why causal is the strongest contract retained under a network partition — with vector-clock dependency-wait as the enforcement mechanism."
reading_time: 7
tags: [consistency, sequential-consistency, causal-consistency, linearizability, cops, van-steen]
sources:
  - title: "Lamport — How to Make a Multiprocessor Computer That Correctly Executes Multiprocess Programs (IEEE TC, 1979)"
    url: "https://lamport.azurewebsites.net/pubs/multi.pdf"
  - title: "Lloyd, Freedman, Kaminsky, Andersen — Don't Settle for Eventual: Scalable Causal Consistency for Wide-Area Storage with COPS (SOSP 2011)"
    url: "https://www.cs.cmu.edu/~dga/papers/cops-sosp2011.pdf"
  - title: "Mahajan, Alvisi, Dahlin — Consistency, Availability, and Convergence (UT Austin TR-11-22)"
    url: "https://www.cs.cornell.edu/lorenzo/papers/cac-tr.pdf"
  - title: "van Steen & Tanenbaum, Distributed Systems (4th ed.), Ch. 7 — Consistency and Replication"
    url: "https://www.distributed-systems.net/index.php/books/ds4/"
  - title: "MongoDB Docs — Causal Consistency and Read and Write Concerns"
    url: "https://www.mongodb.com/docs/manual/core/causal-consistency-read-write-concerns/"
---

**Gist.** A consistency model fixes which execution histories a replicated store may produce; sequential consistency requires a single total order over all operations, while causal consistency requires agreement only on operations linked by happens-before. The weakening is what makes wide-area replication possible: Mahajan, Alvisi and Dahlin prove that nothing stronger than real-time causal consistency is achievable in an always-available, one-way convergent system. The cost is that concurrent writes may be observed in different orders at different replicas, so a separate convergent conflict-handling rule is required to stop replicas diverging permanently.

A consistency model is not code; it is a contract. It fixes, for a replicated store, exactly which sequences of reads and writes the system is allowed to produce. Chapter 7 of van Steen & Tanenbaum stacks the data-centric models by how restrictive that contract is. The two worth separating are **sequential consistency** and **causal consistency**, because the gap between them is the gap between a contract requiring cross-datacenter coordination on the write path and a contract that survives a partition. Logical clocks appear only at the end, as the enforcement mechanism for the weaker contract; the models themselves are defined without any clock.

## Sequential consistency: one global interleaving

Lamport's 1979 definition, written for shared-memory multiprocessors and adopted by the distributed-systems literature, is the reference point. A system is sequentially consistent when

> "the result of any execution is the same as if the operations of all the processors were executed in some sequential order, and the operations of each individual processor appear in this sequence in the order specified by its program."

Two clauses carry the weight. There must exist **one** total order over *all* operations — a single interleaving every process agrees on — and within that order each process's own operations retain their **program order**. The definition says nothing about real time: a read may return a value written later by wall clock, provided some legal interleaving explains every process's observations. That freedom separates sequential consistency from linearizability, which additionally requires the interleaving to respect real-time precedence, so that an operation finishing before another starts also precedes it in the order. Linearizability — also called atomic consistency — is that stronger model; sequential consistency is its non-real-time relaxation.

Consider a single register `x`, two writers and two readers. In the notation below, `W(x)a` writes value `a` and `R(x)b` reads `b`. Time flows left to right within each process, but there is no shared clock across rows.

```
P1:  W(x)a
P2:              W(x)b
P3:                     R(x)b  R(x)a
P4:                     R(x)b  R(x)a
```

This history is sequentially consistent. The single order `W(x)b, W(x)a` explains it: both readers observe `b` then `a`, matching that one interleaving. Perturbing only the readers changes the verdict:

```
P1:  W(x)a
P2:              W(x)b
P3:                     R(x)b  R(x)a
P4:                     R(x)a  R(x)b
```

**No single interleaving exists here.** P3 witnesses `b→a`; P4 witnesses `a→b`. A total order cannot place `W(x)a` both before and after `W(x)b`, so the history is not sequentially consistent. Causal consistency judges it differently.

## Causal consistency: order only what is causally related

Causal consistency weakens the global-order clause to one order *per causal chain*. Writes that are **potentially causally related** must be observed in the same order by every process; writes that are **concurrent** may be observed in any order, and processes may disagree. Causality here is Lamport's happens-before lifted to operations: an operation is causally before another if it precedes it in the same process's program order, if it is a write that a later read observed (a *reads-from* edge), or by transitivity of those two. COPS names the same three rules — execution thread, gets-from, and transitivity — as the definition of its dependency ordering.

Re-judging the second history: `W(x)a` on P1 and `W(x)b` on P2 are **concurrent**, since neither process read the other's value before writing, so no causal edge connects them. Causal consistency permits P3 and P4 to order them oppositely. That history *is* causally consistent although it was shown not to be sequentially consistent, which is the crisp demonstration that **causal consistency is strictly weaker than sequential consistency**.

Making the two writes causally dependent reverses the verdict:

```
P1:  W(x)a
P2:         R(x)a  W(x)b
P3:                        R(x)b  R(x)a
P4:                        R(x)a  R(x)b
```

P2 read `a` and then wrote `b`, so `W(x)a → W(x)b` by a reads-from edge followed by program order. Every process must honour that edge: `a` before `b`. P4 obeys; P3 reports `b` then `a`, contradicting the causal order. This history is **not causally consistent**, and therefore not sequentially consistent either. Causal consistency enforces every real dependency rigidly and relaxes only on genuine concurrency.

| Model | Legal-history rule | Real-time respected? | Agree on concurrent writes? | Available under partition? |
|---|---|---|---|---|
| Linearizable (atomic) | one total order **+** real-time precedence | yes | yes (single order) | no |
| Sequential | one total order, program order preserved | no | yes (single order) | no |
| Causal | one order **per causal chain**; concurrent writes free | no | not required | **yes** |
| Eventual | replicas converge, eventually | no | no | yes |

## Why geo-replication stops at causal

Sequential consistency and linearizability both require a single agreed order over writes, which across datacenters means coordination on the write path, and coordination cannot survive a network partition while the system stays available. This is the wall CAP describes. Causal consistency sits at that availability boundary. Mahajan, Alvisi and Dahlin state it as a two-sided bound: no consistency stronger than real-time causal consistency can be provided in an always-available, one-way convergent system, and real-time causal consistency can be. COPS makes the same point operationally under its ALPS goals — Availability, Low latency, Partition tolerance, Scalability — identifying causal+ consistency as the strongest model achievable under those constraints. **Causal+ is causal consistency plus convergent conflict handling**: a deterministic merge, such as last-writer-wins by timestamp, so that concurrent writes which causal consistency allows replicas to order differently still settle on one value rather than diverging permanently.

## Enforcing it: vector clocks and dependency-wait

The mechanism is shared by COPS's `dep_check` and by classic lazy replication: **tag each write with the causal context it depends on, and refuse to apply a replicated write until every dependency is already present locally.** A vector clock with one entry per replica is a compact summary of that context.

### Implementation sketch (Scala)

```scala
final case class Update(src: Int, key: String, value: String, stamp: Vector[Int])

final class CausalReplica(n: Int, self: Int):
  private var vc: Vector[Int] = Vector.fill(n)(0)   // writes from each replica applied here
  private var queue: List[Update] = Nil             // remote writes awaiting dependencies
  private var store: Map[String, String] = Map.empty

  def localWrite(key: String, value: String): Update =
    vc = vc.updated(self, vc(self) + 1)
    store = store.updated(key, value)
    Update(self, key, value, vc)                    // stamp is this write's causal context

  private def deliverable(u: Update): Boolean =
    // (1) u is the next write expected from its origin; (2) all other deps already applied
    u.stamp(u.src) == vc(u.src) + 1 &&
      (0 until n).forall(k => k == u.src || u.stamp(k) <= vc(k))

  def onRemote(u: Update): Unit =
    queue = u :: queue
    var progress = true
    while progress do                               // applying one update may free others
      progress = false
      queue.filter(deliverable).foreach: r =>
        store = store.updated(r.key, r.value)
        vc = vc.updated(r.src, r.stamp(r.src))
        queue = queue.filterNot(_ eq r)
        progress = true
```

`deliverable` is the entire contract. Condition (1) is the FIFO guarantee: a replica's writes are applied in the order that replica made them. Condition (2) is **dependency-wait**: a write that causally follows an update not yet received waits in `queue` until that update arrives. A remote write is never blocked on a lock, only delayed until its causes land, so local reads do not stall. Convergence for concurrent writes is layered on top: when two updates carry mutually concurrent stamps for the same key, the tie must be broken deterministically — by highest replica identifier, or by a hybrid-logical-clock timestamp — so that every replica settles on the same value. That is the "+" in causal+.

Deployed systems implement this shape. COPS attaches nearest-dependency metadata to each `put_after` and runs `dep_check` at the receiving datacenter before making a version visible. **MongoDB causal-consistent sessions** carry the context as a cluster time: each reply advances the session's `operationTime`, and the next read pins `afterClusterTime`, so a node must have advanced past the session's dependencies before answering. The four session guarantees MongoDB documents — read-own-writes, monotonic reads, monotonic writes, and writes-follow-reads — are causal consistency observed from a single client, and all four hold durably only with `readConcern: "majority"` and `writeConcern: "majority"`.

## Pitfalls

- **Treating causal consistency as "eventually the same".** Causal consistency alone permits two replicas to apply concurrent writes to one key in opposite orders and stay that way; the symptom is two datacenters serving different values indefinitely. Convergent conflict handling, not causal delivery, is what removes it.
- **Assuming sequential consistency implies linearizability.** Sequential consistency admits histories where a read returns a value written after it in real time, so a test that asserts real-time precedence will fail against a correct sequentially consistent store.
- **Omitting condition (2) from the delivery check.** FIFO delivery per origin replica alone allows a write to become visible before the write it read from, reproducing the `R(x)b R(x)a` history that causal consistency forbids.
- **Unbounded dependency queues.** A dropped or delayed update from one replica holds every causally later update in `queue`, and memory grows with the backlog; the symptom is rising queue depth at one replica with reads still served from stale state.
- **Enabling MongoDB causal-consistent sessions with default read and write concerns.** The four session guarantees are documented as durable only under `readConcern: "majority"` with `writeConcern: "majority"`; weaker concerns can expose values that a later election rolls back.
- **Assuming causal metadata is free.** A vector clock carries one entry per replica, so the per-write context grows with the replica count; COPS instead tracks nearest dependencies rather than the full context.
