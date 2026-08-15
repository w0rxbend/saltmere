---
title: "2PC and 3PC: why atomic commit protocols block"
date: 2026-07-26
track: distributed-systems
summary: "Two-phase commit makes a distributed transaction atomic by forcing every participant through a prepare vote before anyone commits, at the cost of blocking indefinitely if the coordinator fails mid-protocol. Three-phase commit adds a pre-commit round that removes blocking under crash-stop failures, but its correctness argument assumes no network partitions. That gap explains why atomic commit is now built on Paxos and Raft."
reading_time: 7
tags: [two-phase-commit, three-phase-commit, atomic-commit, fault-tolerance, consensus, van-steen, distributed-transactions]
sources:
  - title: "Jim Gray & Leslie Lamport, Consensus on Transaction Commit (ACM TODS, 2006)"
    url: "https://dl.acm.org/doi/10.1145/1132863.1132867"
  - title: "Dale Skeen, Nonblocking Commit Protocols (ACM SIGMOD, 1981)"
    url: "https://dl.acm.org/doi/10.1145/582318.582339"
  - title: "Maarten van Steen & Andrew S. Tanenbaum, Distributed Systems, 4th ed."
    url: "https://www.distributed-systems.net/index.php/books/ds4/"
  - title: "Martin Kleppmann, Designing Data-Intensive Applications (ch. 9, Consistency and Consensus)"
    url: "https://www.oreilly.com/library/view/designing-data-intensive-applications/9781491903063/"
---

**Gist.** A distributed transaction touches several participants in separate failure domains and requires a single outcome: all commit, or all abort, since partial commit is corruption. Two-phase commit (2PC) obtains that guarantee by making every participant vote and durably record its vote before any participant applies the decision. The cost is that a participant which has voted commit holds its locks and cannot decide alone, so a coordinator failure at the wrong instant stalls it indefinitely.

## Roles and topology

One node acts as the **coordinator** — a dedicated transaction manager, or the node that initiated the transaction. Every other node touched by the transaction is a **participant** (also called a cohort). The coordinator drives the protocol through explicit phases; participants respond only to the coordinator and do not exchange protocol messages among themselves. This star topology keeps message complexity **linear in the number of participants**, and it makes the coordinator the single node on whose liveness every participant's progress depends.

## Two-phase commit

**Phase 1 — vote/prepare.** The coordinator sends `PREPARE` to every participant. Each participant performs the work required to guarantee that it *can* commit later: validating constraints, acquiring locks, writing undo/redo log records. It then **durably records its vote before sending it**, and replies `VOTE-COMMIT` or `VOTE-ABORT`. Recording first is what makes the vote binding across the participant's own crash and restart.

**Phase 2 — commit/abort.** If every reply is `VOTE-COMMIT`, the coordinator durably logs the decision and sends `GLOBAL-COMMIT`. If any participant votes abort, or fails to reply within the timeout, the coordinator logs and sends `GLOBAL-ABORT`. Participants apply the decision, release locks, and acknowledge.

```
Coordinator                          Participant P
------------                         -------------
state = INIT
send PREPARE to all       ---->      on PREPARE:
                                        if can_commit():
                                          log(VOTE-COMMIT); state = READY
                                          reply VOTE-COMMIT
                                        else:
                                          log(VOTE-ABORT); state = ABORTED
                                          reply VOTE-ABORT

collect votes
if all == VOTE-COMMIT:
  log(GLOBAL-COMMIT); state = COMMIT
  send GLOBAL-COMMIT       ---->      on GLOBAL-COMMIT:
else:                                   commit(); release locks; state = COMMIT
  log(GLOBAL-ABORT); state = ABORT
  send GLOBAL-ABORT        ---->      on GLOBAL-ABORT:
                                        abort(); release locks; state = ABORTED
```

The invariant the protocol maintains is that **no participant applies the decision before the coordinator has durably logged it**, and no participant may retract a commit vote once sent. The state `READY` is therefore an **uncertain state**: the participant has surrendered the right to abort but has not yet acquired the right to commit.

## Why 2PC blocks

Consider a participant `P` in `READY` — it voted commit, holds its locks, and awaits the final decision — when the coordinator fails before phase 2 is transmitted.

- `P` cannot commit unilaterally: another participant may have voted abort, a fact `P` never observes because participants do not talk to each other.
- `P` cannot abort unilaterally: another participant may already have received `GLOBAL-COMMIT` and applied it.

`P` has **no safe default**, so the only correct action is to wait, holding its locks, and with them every transaction that contends for those locks. Polling the surviving participants does not resolve the general case: in the worst case all of them are also in `READY` and hold exactly the information `P` holds. 2PC is therefore a **blocking protocol** — a single coordinator failure at the wrong instant can stall participants for an unbounded time, and resolution requires out-of-band knowledge of the outcome, whether from an operator or from a recovery coordinator that can read the coordinator's log.

## Three-phase commit

Skeen's 1981 paper introduces 3PC by inserting a **pre-commit** phase between voting and committing, so that no participant must infer the outcome from an ambiguous state.

1. **CanCommit** — as in 2PC's prepare: the coordinator asks, participants vote.
2. **PreCommit** — if all votes are commit, the coordinator broadcasts `PRE-COMMIT` and waits for acknowledgements. A participant that reaches `PRE-COMMIT` knows that **every other participant voted commit**; the outcome is settled and only the final signal is outstanding.
3. **DoCommit** — once all acknowledgements are in, the coordinator sends `DO-COMMIT`.

The state space is partitioned so that the two possible outcomes are never simultaneously reachable from a single observed state. If the coordinator fails while some participant is in `PRE-COMMIT`, the survivors can elect a recovery coordinator that drives the transaction to commit, because reaching `PRE-COMMIT` is evidence that the whole cohort voted commit. Conversely, a participant that never reached `PRE-COMMIT` can safely abort, because no participant can have committed. The extra round trip buys this non-blocking property, but **only under Skeen's failure model: crash-stop failures and no network partitions**. The termination protocol depends on the surviving participants being able to communicate with one another, and on an unreachable node being genuinely crashed rather than merely cut off.

That second assumption is where 3PC fails in practice. Under a partition rather than a clean crash, one side may observe enough participants in `PRE-COMMIT` to conclude that commit is safe, while a competing recovery coordinator on the other side observes only participants that never advanced past the vote and concludes that abort is safe. The result is a **split-brain commit/abort disagreement** — the exact inconsistency atomic commit exists to prevent. The protocol removes blocking under a failure model that excludes the failure mode real distributed systems encounter most often, which is the standard explanation given — including in van Steen and Tanenbaum's treatment — for 3PC seeing little production use.

## 2PC compared with 3PC

| | 2PC | 3PC |
|---|---|---|
| Phases | Prepare, Commit | CanCommit, PreCommit, DoCommit |
| Coordinator crash, no partition | Participants in READY block indefinitely | Recovery coordinator resolves via PRE-COMMIT state |
| Network partition | Blocks, as for coordinator crash | Can produce inconsistent commit/abort across partitions |
| Message rounds | 2 | 3 |
| Assumed failure model | Crash-stop | Crash-stop, **no partitions** |
| Durable participant writes before the outcome is known | 1 (vote) | 2 (vote, pre-commit) |
| Production adoption | XA/JTA, distributed SQL commit paths | Rare, mostly academic and internal research systems |

## Why consensus replaced atomic commit

The remedy is not a third phase but a **replicated** decision. Gray and Lamport's Paxos Commit records each participant's prepared-or-aborted vote through a separate instance of Paxos, so the votes are held by a set of acceptors rather than by one coordinator's log. The transaction commits if every participant's instance decides prepared. Because a majority of acceptors retains each vote, no single node's crash leaves the outcome undetermined, and any live majority can recover it. Raft-based systems obtain the same property by making the commit record an entry in a majority-committed replicated log rather than a promise held by one process. The same reasoning underlies the preference for sagas in microservice architectures: where the coordination cost of a consensus-backed commit is unacceptable, the alternative is decomposition into local transactions with compensating actions, not an atomic commit protocol that blocks. Atomic commit across nodes is today generally layered on consensus rather than on the bare 2PC or 3PC handshake, because blocking under partition disqualifies a protocol for any system that must remain available.

### Implementation sketch (Scala)

The load-bearing part of 2PC is not the message passing but the ordering of durable writes against state transitions. The sketch below shows the coordinator side; `log` must return only after the record is durable.

```scala
enum Decision { case Commit, Abort }

trait Participant:
  def prepare(txn: Long): Boolean          // durably records its own vote first
  def decide(txn: Long, d: Decision): Unit

final class Coordinator(log: (Long, Decision) => Unit):

  def run(txn: Long, participants: Vector[Participant]): Decision =
    val votes = participants.map: p =>
      try p.prepare(txn)
      catch case _: Exception => false     // timeout or failure counts as abort

    val decision = if votes.forall(identity) then Decision.Commit else Decision.Abort

    // The decision becomes binding here. A crash before this line means abort on
    // recovery; a crash after it means the recovered coordinator must re-send
    // `decision` until every participant acknowledges.
    log(txn, decision)

    participants.foreach(p => p.decide(txn, decision))
    decision
```

A participant that has returned `true` from `prepare` and not yet received `decide` is in the uncertain state: it must retain its locks and its log record across restart, and it has no correct unilateral action.

## Pitfalls

- **Treating the prepare vote as advisory.** A participant that votes commit and later discovers it cannot apply the change breaks atomicity, because the coordinator may already have committed elsewhere. Every resource the commit needs must be reserved before the vote is sent.
- **Sending the vote before logging it.** A participant that crashes after replying `VOTE-COMMIT` but before the record reaches stable storage recovers with no memory of the vote, and may abort a transaction the coordinator has already committed.
- **Timing out in `READY` and aborting.** A participant that abandons the uncertain state to release locks can abort a transaction other participants have committed. The timeout is a blocking symptom, not a licence to decide.
- **Assuming surviving participants can resolve the outcome.** In the worst case every survivor is also in `READY`; the cohort collectively holds no information about the coordinator's decision.
- **Deploying 3PC across links that partition.** The non-blocking argument holds only under crash-stop failures; with partitions two recovery coordinators can reach opposite decisions.
- **Ignoring lock hold time in the failure analysis.** Blocking is measured not in coordinator downtime alone but in the queue of transactions contending for the locks the blocked participant retains.
- **Treating an XA transaction manager as automatically non-blocking.** XA implements 2PC; recovery still depends on the transaction manager's log surviving and being reachable.
