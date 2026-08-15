---
title: "Ricart–Agrawala: mutual exclusion with no coordinator and no token"
date: 2026-07-31
track: distributed-systems
summary: "Ricart & Agrawala (1981) let N processes share a critical section using only REQUEST and REPLY messages, ordered by Lamport timestamps and enforced by deferring replies. Why the algorithm needs a logical clock, how deferral guarantees safety, and why 2(N−1) messages is optimal for a permission-based scheme."
reading_time: 6
tags: [mutual-exclusion, ricart-agrawala, coordination, lamport-clocks, distributed-systems]
sources:
  - title: "An Optimal Algorithm for Mutual Exclusion in Computer Networks (CACM 24(1):9–17, 1981) — Glenn Ricart & Ashok K. Agrawala"
    url: "https://dl.acm.org/doi/10.1145/358527.358537"
  - title: "Ricart–Agrawala algorithm — Wikipedia (message types, deferral, 2(N-1) cost)"
    url: "https://en.wikipedia.org/wiki/Ricart%E2%80%93Agrawala_algorithm"
  - title: "Distributed Systems (4th ed.) — van Steen & Tanenbaum, Ch. 6 Coordination"
    url: "https://www.distributed-systems.net/index.php/books/ds4/"
  - title: "Lamport's logical clocks and Ricart–Agrawala in Python — bytepawn (engineering write-up)"
    url: "https://bytepawn.com/lamport-logical-clocks-distributed-mutual-exclusion.html"
  - title: "Ricart–Agrawala Algorithm in Mutual Exclusion in Distributed System — GeeksforGeeks"
    url: "https://www.geeksforgeeks.org/operating-systems/ricart-agrawala-algorithm-in-mutual-exclusion-in-distributed-system/"
---

**Gist.** Mutual exclusion across a network cannot rely on shared memory or a shared clock, so processes must agree by exchanging messages on which of them may enter a critical section (CS) next. Ricart–Agrawala (1981) solves this with two message types — REQUEST and REPLY — and a single rule: a process enters only after every other process has replied, and a process withholds its reply while it holds, or outranks the sender for, the critical section. The cost is **2(N−1) messages per entry** and a dependency on every peer being alive, since one silent process stalls the entire group.

## Three shapes of critical-section guard

van Steen & Tanenbaum's coordination chapter groups the classical answers as follows.

- **Centralized coordinator.** One process acts as lock server: request, wait for a grant, send a release on exit — three messages per entry. The coordinator is a single point of failure and a throughput bottleneck, and its crash is indistinguishable from a denial, because both appear to a requester as the absence of a grant.
- **Token ring.** A single token circulates a logical ring; possession confers the right to enter. Starvation is excluded, but a lost token must be regenerated with certainty that the old one no longer exists, and a process may wait a full circulation of the ring even when no other process wants the CS.
- **Permission-based (Ricart–Agrawala).** No coordinator and no token. A process asks all peers and enters only when all peers have consented.

## Two message types, one logical clock

A process wishing to enter the CS broadcasts a **REQUEST** carrying `(timestamp, pid)` to the other **N−1** processes. Each recipient eventually returns a **REPLY**. Once REPLYs from all N−1 peers have been collected, the requester enters. The entire subtlety lies in *when* a recipient replies.

The timestamp is a **Lamport logical clock** value, and it is load-bearing rather than decorative. Without a global clock, two requests may be concurrent in the happens-before sense, and the algorithm requires a **total order** on requests so that every conflict resolves to the same winner at every node. Lamport timestamps supply a partial order consistent with causality; the pair `(timestamp, pid)`, compared lexicographically with the process identifier as tiebreaker, extends it to a total order because process identifiers are unique.

The consequence of removing the clock is not a performance loss but a correctness loss. If two competing requests are incomparable, each recipient is free to resolve the conflict its own way: **both processes may defer to each other (deadlock), or both may collect a full set of REPLYs and enter together (a safety violation).** The total order is what makes the local decision rule globally consistent.

## Deferred replies are the lock

On receiving a REQUEST, a process is in exactly one of three states, and the state determines the response.

- **Not interested in the CS.** REPLY immediately.
- **Inside the CS.** Defer — hold the REPLY until exit.
- **Also requesting.** Compare `(timestamp, pid)` pairs. If the incoming request is *lower*, it has priority and the recipient REPLYs now. Otherwise the recipient's own request wins and it **defers**.

Deferred replies are the lock itself: there is no lock variable anywhere in the system, only an outstanding REPLY that has not yet been sent.

The safety invariant follows. **The requester whose `(timestamp, pid)` is lowest among all outstanding requests — including that of any process currently inside the CS — is outranked by no one, so no peer has grounds to defer, and it collects all N−1 REPLYs.** Every competitor with a higher pair is missing at least that process's REPLY and therefore blocks. Because the order is total and every process applies the same comparison, at most one requester can hold the minimum at any moment, so at most one can assemble a complete reply set. On exit, the holder flushes every deferred REPLY, which releases the next-lowest requester.

Liveness follows from the same order. A given request's timestamp is fixed at the moment it is issued, and only finitely many requests can carry a lower pair, so each request is eventually the minimum among outstanding requests. Entry is therefore granted in timestamp order and no process starves.

### Implementation sketch (Scala)

```scala
type Pid = Int
final case class Stamp(ts: Long, pid: Pid)
object Stamp:
  given Ordering[Stamp] = Ordering.by(s => (s.ts, s.pid))

enum Msg:
  case Request(stamp: Stamp)
  case Reply(from: Pid)
import Msg.*

final class Node(val pid: Pid, peers: Set[Pid], send: (Pid, Msg) => Unit):
  private var clock: Long = 0
  private var myReq: Option[Stamp] = None
  private var inCs: Boolean = false
  private var replies: Set[Pid] = Set.empty
  private var deferred: Set[Pid] = Set.empty

  def requestCs(): Unit = synchronized:
    clock += 1
    myReq = Some(Stamp(clock, pid))
    replies = Set.empty
    peers.foreach(p => send(p, Request(myReq.get)))

  def onRequest(from: Stamp): Unit = synchronized:
    clock = math.max(clock, from.ts) + 1        // Lamport receive rule
    // Withhold consent while holding the CS, or while outranking the sender.
    val hold = inCs || myReq.exists(Ordering[Stamp].lt(_, from))
    if hold then deferred += from.pid else send(from.pid, Reply(pid))

  def onReply(from: Pid): Unit = synchronized:
    replies += from
    if replies == peers then                    // consent from every peer
      inCs = true

  def releaseCs(): Unit = synchronized:
    inCs = false
    myReq = None
    deferred.foreach(p => send(p, Reply(pid)))   // flush held consents
    deferred = Set.empty
```

The `synchronized` blocks stand in for whatever single-threaded event loop delivers messages; the algorithm assumes each handler runs to completion before the next message is processed.

## Message cost and the meaning of "optimal"

Each entry costs one REQUEST and one REPLY to each of the N−1 peers: **2(N−1) messages per critical-section access**. The synchronization delay — the gap between one process leaving the CS and the next entering — is a single message transmission, since the deferred REPLY flushed on exit is the last consent the successor was waiting for. In a scheme where every process participates in every decision, one message in each direction per peer is the minimum; the 1981 paper's title claims optimality in that message-count sense.

Against the coordinator's flat three messages, the cost scales poorly: every entry touches the whole group, so message traffic grows linearly with N while the coordinator's stays constant. What the extra traffic buys is a symmetric system with no single point of failure and an explicit total order that yields timestamp-ordered fairness and freedom from starvation.

The exposure is fault tolerance, and it is worse than the coordinator's. **A single crashed process never sends its REPLY, so every subsequent entry attempt by any process stalls** — the algorithm requires N−1 consents and accepts no substitute. Availability therefore decreases as N grows, since more processes means more processes that can fail. Maekawa's algorithm reduces the requirement to a quorum of roughly √N processes rather than all of them, and failure detection can be layered on top; production systems commonly use a lease held in a consensus service instead. As a demonstration that mutual exclusion needs *ordering* rather than a *master*, the algorithm remains instructive.

## Pitfalls

- **Omitting the pid tiebreaker.** Two processes stamp their requests with the same Lamport value, each finds the other's request neither lower nor higher, and both either defer (deadlock) or both enter (lost updates in the critical section).
- **Advancing the clock only on send.** A receiver that does not apply `clock = max(clock, incoming) + 1` can issue a request carrying a lower timestamp than one it has already seen, so the total order stops respecting happens-before; mutual exclusion survives, because the lexicographic comparison is still total, but a request can jump ahead of one that causally preceded it and a slow-clocked process can win repeatedly.
- **Replying immediately while inside the critical section.** The process appears correct in isolation, then a peer enters concurrently; a shared counter increments fewer times than the number of entries, and the loss is visible only under contention.
- **Clearing the requesting flag before flushing deferrals.** Held REPLYs never arrive and the deferred peers wait indefinitely, with no error surfaced anywhere — the symptom is a silent hang, not a failure.
- **Treating a crashed peer as a slow peer.** The requester waits for an N−1st REPLY that will never come; because the algorithm has no timeout or release path, the group wedges permanently rather than degrading.
- **Assuming the reply set can be counted rather than tracked by identity.** A duplicated REPLY from one peer under message retransmission satisfies a counter comparison while a different peer's consent is still outstanding, permitting entry without full permission.
