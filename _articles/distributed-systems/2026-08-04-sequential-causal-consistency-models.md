---
title: "Sequential vs causal consistency: which histories are legal, and why geo-replication settles for causal"
date: 2026-08-04
track: distributed-systems
summary: "Data-centric consistency models are contracts over the set of legal execution histories. Sequential consistency (Lamport, 1979) demands one global interleaving; causal consistency only orders writes that are actually causally related. This article decides concrete histories under each model, shows exactly where they diverge, and explains why causal is the strongest thing you can keep under a network partition — with vector-clock dependency-wait as the enforcement mechanism."
reading_time: 6
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

A consistency model is not code; it is a *contract*. It fixes, for a replicated store, exactly which execution histories are legal — which sequences of reads and writes the system is allowed to produce. Chapter 7 of van Steen & Tanenbaum stacks the data-centric models by how restrictive that contract is. The two worth pulling apart are **sequential consistency** and **causal consistency**, because the gap between them is precisely the gap between "correct but slow across a WAN" and "the strongest thing you can still serve during a partition." Logical clocks show up here only at the end, as the mechanism that enforces the weaker contract — the models themselves are defined without any clock at all.

## Sequential consistency: one global interleaving

Lamport's 1979 definition, written for shared-memory multiprocessors but adopted wholesale by the distributed-systems community, is the reference point. A system is sequentially consistent when

> "the result of any execution is the same as if the operations of all the processors were executed in some sequential order, and the operations of each individual processor appear in this sequence in the order specified by its program."

Two clauses do all the work. There must exist **one** total order over *all* operations (a single interleaving every process agrees on), and within that order each process's own operations keep their **program order**. Crucially, the definition says nothing about real time: a read may return a value written "later" by wall clock, as long as some legal interleaving explains every process's observations. That freedom is what separates sequential consistency from linearizability, which additionally requires the interleaving to respect real-time precedence (if op A finishes before op B starts, A precedes B). Linearizability is *strict*/atomic; sequential consistency is its non-real-time relaxation.

Consider a single register `x`, two writers, two readers. Notation: `W(x)a` writes value `a`; `R(x)b` reads `b`. Time flows left to right per process, but there is no shared clock across rows.

```
P1:  W(x)a
P2:              W(x)b
P3:                     R(x)b  R(x)a
P4:                     R(x)b  R(x)a
```

This is sequentially consistent. Pick the single order `W(x)b, W(x)a`; both readers observe `b` then `a`, consistent with that one interleaving. Now perturb only the readers:

```
P1:  W(x)a
P2:              W(x)b
P3:                     R(x)b  R(x)a
P4:                     R(x)a  R(x)b
```

There is **no** single interleaving here. P3 witnesses `b→a`; P4 witnesses `a→b`. A total order cannot place `W(x)a` both before and after `W(x)b`. So this history is **not** sequentially consistent. Hold onto it — causal consistency will judge it differently.

## Causal consistency: only order what's causally related

Causal consistency weakens the "one global order" clause to "one order *per causal chain*." Writes that are **potentially causally related** must be seen in the same order by every process; writes that are **concurrent** may be seen in any order, and different processes may disagree. Causality here is exactly Lamport's happens-before, lifted to operations: an operation is causally before another if it precedes it in the same process's program order, if it is a write that a later read observed (a *reads-from* edge), or by transitivity of those two. COPS names these same three rules — execution thread, gets-from, and transitivity — as the definition of its `❀` ordering.

Re-judge the second history. `W(x)a` (P1) and `W(x)b` (P2) are **concurrent**: neither process read the other's value before writing, so there is no causal edge between them. Causal consistency therefore permits P3 and P4 to order them oppositely. That history *is causally consistent* — even though we just proved it is not sequentially consistent. This single example is the crisp proof that **causal is strictly weaker than sequential**.

Now make the two writes causally dependent and watch causal consistency bite:

```
P1:  W(x)a
P2:         R(x)a  W(x)b
P3:                        R(x)b  R(x)a
P4:                        R(x)a  R(x)b
```

P2 read `a` and *then* wrote `b`, so `W(x)a → W(x)b` (reads-from, then program order). Every process must now honor that single edge: `a` before `b`. P4 obeys; P3 reports `b` then `a`, contradicting the causal order. This history is **not causally consistent** (and a fortiori not sequential). The lesson: causal consistency is not "anything goes for concurrent writes and nothing else" — it rigidly enforces every real dependency, and only relaxes on genuine concurrency.

| Model | Legal-history rule | Real-time respected? | Agree on concurrent writes? | Available under partition? |
|---|---|---|---|---|
| Linearizable (strict/atomic) | one total order **+** real-time precedence | yes | yes (single order) | no |
| Sequential | one total order, program order preserved | no | yes (single order) | no |
| Causal | one order **per causal chain**; concurrent writes free | no | not required | **yes** |
| Eventual | replicas converge, eventually | no | no | yes |

## Why geo-replication stops at causal

Sequential consistency and linearizability both require a single agreed order over writes, which across datacenters means coordination on the write path — and coordination cannot survive a network partition while staying available. This is the wall CAP describes. Causal consistency is special because it sits exactly at the availability boundary. Mahajan, Alvisi, and Dahlin prove it as a two-sided bound: no consistency stronger than real-time causal "can be provided in an always-available, one-way convergent system," and real-time causal *can* be. COPS makes the same point operationally under its ALPS goals (Availability, Low latency, Partition tolerance, Scalability), calling causal+ consistency the strongest model achievable under those constraints. "Causal+" is causal consistency plus **convergent conflict handling** — a deterministic merge (e.g., last-writer-wins by timestamp) so concurrent writes, which causal consistency lets replicas order differently, still converge to one value rather than diverging forever. That is the CALM intuition too: causal delivery is monotone bookkeeping, so it needs no coordination.

## Enforcing it: vector clocks and dependency-wait

The mechanism is the same idea in COPS's `dep_check` and in classic lazy-replication: **tag each write with the causal context it depends on, and refuse to apply a replicated write until every dependency is already present locally.** With one entry per replica, a vector clock is a complete, compact summary of that context.

```python
class CausalReplica:
    def __init__(self, n, i):
        self.i, self.n = i, n
        self.vc = [0] * n          # writes from each replica applied here
        self.queue = []            # remote writes waiting on dependencies

    def local_write(self, key, val):
        self.vc[self.i] += 1
        stamp = list(self.vc)              # this write's causal context
        self.apply(key, val)
        broadcast(Update(self.i, key, val, stamp))

    def deliverable(self, u):
        # 1) u is the very next write we expect from its origin
        if u.stamp[u.src] != self.vc[u.src] + 1:
            return False
        # 2) every OTHER dependency of u is already applied here
        return all(u.stamp[k] <= self.vc[k]
                   for k in range(self.n) if k != u.src)

    def on_remote(self, u):
        self.queue.append(u)
        progress = True
        while progress:                    # cascade: applying one may free others
            progress = False
            for u in list(self.queue):
                if self.deliverable(u):
                    self.apply(u.key, u.val)
                    self.vc[u.src] = u.stamp[u.src]
                    self.queue.remove(u)
                    progress = True
```

`deliverable` is the whole contract. Condition (1) is the FIFO guarantee — you apply a replica's writes in the order it made them. Condition (2) is **dependency-wait** — a write that causally follows something you haven't seen yet sits in `queue` until that something arrives. A remote write is never *blocked on a lock*, only *delayed until its causes land*, which is why reads never stall. Convergence for concurrent writes is layered on top: when two updates carry mutually concurrent stamps to the same key, break the tie deterministically (highest replica-id, or a hybrid-logical-clock timestamp) so every replica lands on the same value — the "+" in causal+.

Real systems ship this. COPS attaches nearest-dependency metadata to each `put_after` and runs `dep_check` at the receiving datacenter before making a version visible. **MongoDB causal-consistent sessions** carry the context as a cluster time: each reply advances the session's `operationTime`, and the next read pins `afterClusterTime` so a node must have advanced past your dependencies before answering. The four session guarantees MongoDB lists — read-your-writes, monotonic reads, monotonic writes, writes-follow-reads — are exactly causal consistency observed from one client, and you only get all four *durably* with `readConcern: "majority"` and `writeConcern: "majority"`.

**Try next:** Take the `CausalReplica` above, spin up three instances, and drive the second causal-violation history through them — P1 writes `a`, a second replica reads `a` then writes `b`. Now deliver `b` to the third replica *before* `a` and assert it lands in `queue` rather than becoming visible; then deliver `a` and watch the cascade release `b`. Finally, delete condition (2) from `deliverable` and reproduce the illegal `R(x)b R(x)a` read — proving that dependency-wait, not FIFO alone, is what buys you causal consistency.
