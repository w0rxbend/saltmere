---
title: "Edge-chasing: detecting distributed deadlock without ever building the graph"
date: 2026-07-31
track: distributed-systems
summary: "The Chandy–Misra–Haas AND-model algorithm finds a cycle in a wait-for graph that no single node can see. Blocked processes chase probes along their wait edges; a probe that returns to its initiator proves a deadlock, and the full graph is never assembled."
reading_time: 6
tags: [deadlock-detection, edge-chasing, chandy-misra-haas, wait-for-graph, coordination, distributed-systems]
sources:
  - title: "Chandy, Misra, Haas — Distributed Deadlock Detection (ACM TOCS, 1983)"
    url: "https://dl.acm.org/doi/10.1145/357360.357365"
  - title: "Chandy, Misra, Haas — Distributed Deadlock Detection (paper PDF)"
    url: "https://cse.iitkgp.ac.in/~agupta/distsys/Deadlock-ChandyMishraHaas.pdf"
  - title: "Kshemkalyani & Singhal — Distributed Computing, Ch. 10: Deadlock Detection in Distributed Systems"
    url: "https://www.cs.uic.edu/~ajayk/Chapter10.pdf"
  - title: "Wikipedia — Chandy–Misra–Haas algorithm (resource model)"
    url: "https://en.wikipedia.org/wiki/Chandy%E2%80%93Misra%E2%80%93Haas_algorithm_resource_model"
  - title: "van Steen & Tanenbaum — Distributed Systems (3rd ed.), coordination chapter"
    url: "https://www.distributed-systems.net/index.php/books/ds3/"
---

**Gist.** A distributed deadlock is a cycle in a wait-for graph whose edges are scattered across nodes, so no participant can inspect the graph and no coordinator can assemble a consistent copy of it. The Chandy–Misra–Haas (CMH) edge-chasing algorithm replaces the graph with messages: a blocked process sends a small **probe** along each of its wait-for edges, other blocked processes forward it, and a probe arriving back at its own initiator proves a cycle. The cost is that detection is per-initiator and reactive — the cycle is discovered only after a process has already blocked and a hunt has been started, and the probe traffic is paid again for every initiator.

## The wait-for graph and its distribution

A deadlock is a cycle in the **wait-for graph** (WFG): process P1 is blocked on a resource held by P2, P2 on P3, …, Pn on P1. On a single machine the operating system holds the entire graph and runs a cycle check over it. When the processes are spread across nodes, the graph is spread with them: **each node holds only the outgoing edges of the processes it hosts**. No participant holds a cycle, and the direct remedy — shipping every local slice to a coordinator — introduces a different defect.

## Why centralised collection fails

Local WFG slices reach the coordinator over links with finite but unpredictable delay, and no global clock exists to align the instants at which the slices were taken. The coordinator therefore reasons over a **union of snapshots captured at different times**.

That union admits **phantom (false) deadlocks**. Suppose the coordinator holds a stale edge P1→P2 together with a fresh edge P2→P1. If P2 released its resource before P1's edge was reported, the two edges never existed simultaneously and no cycle ever formed; the coordinator nevertheless observes a cycle and aborts a process that was not deadlocked. **A detector must report only cycles that hold simultaneously**, and a union of unsynchronised snapshots cannot establish simultaneity.

The absent global clock also constrains the other two strategies. Prevention — acquiring all resources in one step, or preempting holders — is expensive across a network. Avoidance requires accurate global state at each grant decision to test whether the grant leaves the system in a safe state, which is precisely the state that cannot be obtained cheaply. What remains in practice is to permit deadlocks and detect them.

## The AND request model

CMH as described here targets the **AND model**: a process may request several resources at once and remains blocked until *all* of them are granted. Consequently a blocked process has an outgoing wait-for edge to **every** process it awaits, and it stays blocked until every one of those edges clears. This is the request shape of a database transaction holding some locks and queuing for several more.

## Probes and the detection invariant

Rather than collecting edges, CMH traverses them with messages called **probes**. A probe carries three process identifiers:

```
probe(i, j, k)   # i = initiator, j = sender, k = receiver
```

The reading is: the hunt initiated by Pi has reached Pj, which forwards it to Pk because Pj is blocked waiting on Pk. Two restrictions make the traversal sound. **Probes travel only along wait-for edges**, and **only blocked processes forward them** — a running process lies on no cycle, so the chase terminates at it.

The detection rule is the invariant of the algorithm: **when a process receives a probe whose initiator field equals its own identifier, the probe has traversed a closed chain of wait-for edges, every hop of which was blocked at the moment it forwarded, so that chain is a deadlock.** No node ever materialises the cycle; the cycle is witnessed by the message returning.

The state each blocked process keeps is small: the set of processes it waits on, and a **`dependent` set of initiators already forwarded**. The second set is what bounds the traffic and guarantees termination — a blocked process propagates a given initiator's probe at most once per outgoing edge, so a probe cannot circulate indefinitely around a cycle. The published bound for the resource model is **at most `m(n − 1)/2` messages to detect a deadlock, for `m` processes spread over `n` nodes**, with a detection delay of O(n). What is never paid is a transfer of the graph itself.

### Implementation sketch (Scala)

```scala
final case class Probe(initiator: Int, sender: Int, receiver: Int)

final class Process(val pid: Int, send: (Int, Probe) => Unit):
  private var blocked: Boolean = false
  private var waitsFor: Set[Int] = Set.empty   // AND model: all must clear
  private var dependent: Set[Int] = Set.empty  // initiators already forwarded

  def blockOn(targets: Set[Int]): Unit =
    blocked = true
    waitsFor = targets

  def release(): Unit =
    blocked = false
    waitsFor = Set.empty
    dependent = Set.empty

  /** Started by the local controller for a process that has been blocked
    * long enough to be worth investigating. */
  def initiate(): Unit =
    if blocked then waitsFor.foreach(k => send(k, Probe(pid, pid, k)))

  def onProbe(p: Probe): Option[Int] =
    if !blocked then None                       // running: chase dies here
    else if p.initiator == pid then Some(pid)   // probe came home: deadlock
    else if dependent.contains(p.initiator) then None
    else
      dependent += p.initiator
      waitsFor.foreach(k => send(k, Probe(p.initiator, pid, k)))
      None
```

The forwarding branch is O(1) work plus one message per outgoing edge, and `dependent` prunes every repeat of the same hunt.

## What the absence of a graph buys

Edge-chasing removes both defects of the coordinator at once. There is no central snapshot that can go stale, so no cycle can be inferred from edges that never coexisted: a probe returns to its initiator only if a chain of processes, each blocked when it forwarded, links back to that initiator. There is also no bulk transfer of state — the traversal runs on the nodes that own the edges, each performing a local step and passing a three-field message onward. A global property, the existence of a cycle, is decided through local decisions and small messages.

One consequence deserves naming: **detection is initiator-scoped**. The algorithm reports the deadlock to the process whose probe returned, and a cycle containing several initiators can be detected several times, once per initiator whose hunt completes. Choosing which participant to abort, and ensuring only one is aborted, is a separate decision the algorithm does not make.

## Pitfalls

- Forwarding probes from a **running** process reintroduces phantom deadlocks: the chain is then no longer a chain of simultaneously blocked processes, and a returned probe no longer proves a cycle.
- Omitting the `dependent` set leaves probes circulating around a cycle indefinitely and inflates the message count past the published bound, because every lap re-forwards the same initiator on every edge.
- Failing to clear `dependent` when a process unblocks suppresses later hunts: a subsequent, genuine deadlock involving the same initiator is silently not forwarded, and the probe dies at the stale entry.
- Treating the AND-model rule as universal misdetects under OR-style requests, where a process needs any one of several resources; a returned probe there does not imply that every member of the cycle is permanently stuck.
- Reporting the deadlock without a deterministic victim rule lets multiple detections of the same cycle abort several processes instead of the one needed to break it.
- Starting a hunt for every process the instant it blocks makes probe traffic scale with ordinary lock contention rather than with deadlock frequency, since most blocked processes are waiting on progress rather than on a cycle.
