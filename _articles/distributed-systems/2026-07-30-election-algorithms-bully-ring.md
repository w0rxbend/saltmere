---
title: "Bully and Ring Election: the classic coordinator algorithms"
date: 2026-07-30
track: distributed-systems
summary: "Before Raft there were the Bully algorithm (Garcia-Molina, 1982) and the Ring algorithm — two ways for a group of numbered processes to agree on the highest-id survivor as coordinator. Both are given here with handlers, a crash walk-through, and the message counts that separate them."
reading_time: 6
tags: [leader-election, bully-algorithm, ring-algorithm, coordination, garcia-molina, message-complexity]
sources:
  - title: "Elections in a Distributed Computing System (IEEE Transactions on Computers, C-31, 1982) — Hector Garcia-Molina"
    url: "https://dl.acm.org/doi/10.1109/TC.1982.1675885"
  - title: "Distributed Systems (4th ed.) — van Steen & Tanenbaum, Ch. 6 Coordination"
    url: "https://www.distributed-systems.net/index.php/books/ds4/"
  - title: "Bully algorithm — Wikipedia (assumptions, messages, O(n^2) worst case)"
    url: "https://en.wikipedia.org/wiki/Bully_algorithm"
  - title: "Chang and Roberts algorithm — Wikipedia (ring election, 3N-1 worst case)"
    url: "https://en.wikipedia.org/wiki/Chang_and_Roberts_algorithm"
  - title: "CS677 Lecture 14: Leader Election (lecture notes) — Prashant Shenoy, UMass"
    url: "https://lass.cs.umass.edu/~shenoy/courses/spring22/lectures/Lec14_notes.pdf"
---

**Gist.** Many distributed algorithms require exactly one process to hold a distinguished role — lock server, sequencer, replication primary — and that role must be reassigned without human intervention when its holder crashes. **Leader (coordinator) election** solves this by having the survivors converge on a single winner, and the two textbook algorithms, **Bully** (Garcia-Molina, 1982) and **Ring**, both do so by electing the highest-id live process. The cost is a synchrony assumption: both rely on bounded message delay and timeout-based failure detection, so under a network partition each side can elect its own coordinator.

## The setup

Both algorithms assume `n` processes, each carrying a **unique, comparable identifier**. Bully additionally requires every process to know the identifiers and addresses of all the others; Ring requires only that each process can reach its successor. The election rule is deliberately trivial — **the highest-id process currently alive becomes coordinator** — which isolates the genuinely hard part: detecting the old coordinator's death and converging on the replacement.

That detection is what forces the **synchronous** model. Failure is inferred from silence: "no reply within a timeout `T`" is treated as "dead". A **failure detector built on timeouts cannot distinguish a crashed process from a slow or unreachable one**, so the safety of both algorithms depends on the delay bound holding. Raft's terms and randomized election timeouts, and quorum-based leases, address the same problem under weaker assumptions.

## The Bully algorithm

Three message types carry the protocol: **ELECTION** (an election is starting; sent only to strictly higher ids), **OK / ANSWER** (the recipient is alive and outranks the sender, which must stand down), and **COORDINATOR** (the sender has won). Any process observing that the coordinator is unreachable may start an election.

The per-process state machine has three states. In **normal** operation the process holds a recorded coordinator id. On starting an election it moves to **awaiting-OK**: it sends ELECTION to every higher id and arms a timer for `T`. Two transitions leave that state. If **any** OK arrives, the process moves to **awaiting-COORDINATOR** — some higher process is alive and will resolve the election — and arms a second timer. If **no** OK arrives before `T` expires, the process **declares victory**: it records itself as coordinator and broadcasts COORDINATOR to all peers.

The second timer is what keeps the algorithm live. A process that stood down after an OK has no further obligation, but the process that silenced it may itself crash before announcing a winner. **Expiry of the awaiting-COORDINATOR timer restarts the whole election**, so a crash during an election is recovered by another election rather than leaving the group leaderless.

The invariant the algorithm maintains is one-directional: **a process only ever concedes to a strictly higher id, and only ever declares victory when every higher id has failed to answer.** Under the synchrony assumption, exactly one process can satisfy the victory condition, because the highest live id always answers everyone below it and is answered by no one.

## Walk-through: the top node crashes

Take five processes with ids `{1,2,3,4,5}`, where `5` is coordinator. `5` crashes, and `2` observes it first because a request to `5` times out.

1. `2` sends ELECTION to `3`, `4`, `5`.
2. `3` and `4` reply **OK**; `5` is silent. `2` stands down and waits for a COORDINATOR message.
3. Having answered, `3` and `4` each start their own election. `3` sends ELECTION to `4` and `5`; `4` replies OK, so `3` stands down. `4` sends ELECTION to `5` alone.
4. `4` receives nothing before `T`. Its timer fires, `4` **declares victory**, and it sends COORDINATOR to `1`, `2`, `3`, `5`.
5. All survivors record `4`. When `5` reboots it starts an election, wins on its identifier, and resumes the role.

The message count depends on who initiates. **When the lowest id initiates, the algorithm costs O(n²) messages**, because each process that receives an ELECTION starts its own election toward every higher id. **When the process immediately below the dead coordinator initiates, the cost is O(n)**: a small number of ELECTIONs, one round of OKs, one COORDINATOR broadcast.

## The Ring algorithm

The processes are arranged in a **logical ring**, where each knows only its successor and skips failed successors to the next live one. Election messages travel in one direction, carrying an accumulating list of identifiers. The ELECTION message circulates once, gathering every live id; when it returns to the initiator — detected because the initiator's own id is already in the list — the initiator computes `max(ids)` and sends a **COORDINATOR** message around the ring to announce the winner and terminate the round.

The two laps — one to collect the identifiers, one to announce the winner — cost **O(n) messages**, and the count does not depend on which process starts. The **Chang–Roberts** variant forwards only identifiers larger than the receiver's own and discards smaller ones, so the circulating message shrinks as it travels; its worst case is **3n−1 sequential messages**, the same linear order.

### Implementation sketch (Scala)

```scala
enum Msg:
  case Election(src: Int)
  case Ok(src: Int)
  case Coordinator(src: Int)
  case RingElection(ids: List[Int])
  case RingCoordinator(leader: Int)

class Node(
    val id: Int,
    peers: Map[Int, Msg => Unit],
    successor: Msg => Unit,
    timer: (Long, () => Unit) => Unit,
    T: Long):
  private var coordinator: Option[Int] = None
  private var awaitingOk = false

  def startElection(): Unit =
    val higher = peers.keys.filter(_ > id).toList
    if higher.isEmpty then declareVictory()
    else
      awaitingOk = true
      higher.foreach(p => peers(p)(Msg.Election(id)))
      timer(T, () => if awaitingOk then declareVictory()) // no higher id answered

  def receive(m: Msg): Unit = m match
    case Msg.Election(src) =>
      peers(src)(Msg.Ok(id))              // src is strictly lower by construction
      if !awaitingOk then startElection()
    case Msg.Ok(_) =>
      awaitingOk = false
      timer(T, () => if coordinator.isEmpty then startElection()) // winner never announced
    case Msg.Coordinator(src) =>
      coordinator = Some(src)
    case Msg.RingElection(ids) =>
      if !ids.contains(id) then successor(Msg.RingElection(id :: ids))
      else successor(Msg.RingCoordinator(ids.max))
    case Msg.RingCoordinator(leader) =>
      coordinator = Some(leader)
      if leader != id then successor(Msg.RingCoordinator(leader))

  private def declareVictory(): Unit =
    coordinator = Some(id)
    peers.foreachEntry((p, send) => if p != id then send(Msg.Coordinator(id)))
```

## Comparison

| | Bully | Ring |
|---|---|---|
| Messages (worst case) | **O(n²)** (lowest id starts) | **O(n)** (two laps) |
| Messages (best case) | O(n) | O(n) (two laps) |
| Topology known | full membership | successor only |
| Convergence | one timeout when a high id is alive | one guaranteed round-trip |
| Failure model | synchronous + timeouts | synchronous + timeouts |

Bully converges in fewer timeout periods when a high-id survivor exists, at the cost of message growth when a low id initiates. Ring pays predictable linear traffic but must repair the ring when nodes fail and always pays two full laps.

Both share the limitation that quorum-based protocols remove: they assume reliable delivery and bounded timeouts, so **a network partition can produce two coordinators**, one on each side, each having observed silence from every higher id. Neither algorithm carries a term number or a quorum condition that would let a group reject the losing coordinator's writes.

## Pitfalls

- **Lost OK message crowns the wrong node.** A single dropped ANSWER leaves a lower-id process with no evidence that a higher one is alive; its timer fires and it broadcasts COORDINATOR while the higher process is still running.
- **Partition yields two coordinators.** Each side elects its own highest live id, and nothing in either protocol invalidates the smaller side's winner when the partition heals.
- **Missing awaiting-COORDINATOR timer leaves the group leaderless.** A process that stands down after an OK will wait indefinitely if the process that silenced it crashes before announcing a winner.
- **Lowest-id initiator is the O(n²) case.** Detection order is not controlled by the algorithm, so the expensive path is triggered by whichever process happens to time out first, not by a configuration choice.
- **A rebooting high id preempts a working coordinator.** Because the rule is highest-id-alive, the returning process starts an election and takes the role from an incumbent that was operating correctly.
- **A stale ring topology loops or drops messages.** If a process forwards to a successor that has failed and no repair advances the pointer to the next live node, the ELECTION message never completes its lap and the round never terminates.
