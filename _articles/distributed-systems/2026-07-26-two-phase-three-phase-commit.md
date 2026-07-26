---
title: "2PC and 3PC: why atomic commit protocols block (and mostly lost)"
date: 2026-07-26
track: distributed-systems
summary: "Two-phase commit makes a distributed transaction atomic by forcing every participant through a prepare vote before anyone commits — at the cost of blocking forever if the coordinator dies mid-protocol. Three-phase commit adds a pre-commit round to fix that under crash-stop failures, then falls apart the moment the network partitions. That gap is exactly why Paxos and Raft replaced atomic commit as the tool of choice."
reading_time: 5
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

A distributed transaction touches several participants — separate databases, separate services, separate failure domains — and needs a single outcome: every participant commits, or every participant aborts. Partial commit is corruption. **Atomic commit protocols** exist to make that all-or-nothing guarantee mechanical, without requiring the participants to trust each other's intentions. Two-phase commit (2PC) is the classic answer; three-phase commit (3PC) is the textbook fix for 2PC's biggest flaw, and understanding why 3PC's fix itself fails is what explains why the field moved to consensus protocols like Paxos and Raft instead.

## Roles: coordinator and participants

One node is the **coordinator** (sometimes a dedicated transaction manager, sometimes just the node that initiated the transaction). Every other node touched by the transaction is a **participant** (also called a cohort). The coordinator drives the protocol through explicit phases; participants only respond to the coordinator's messages and never talk directly to each other. This star topology is deliberate — it keeps the message complexity linear in the number of participants, but it also means the coordinator is a single point of control that every participant's fate depends on.

## Two-phase commit

**Phase 1 — Vote/Prepare.** The coordinator sends `PREPARE` to every participant. Each participant does whatever work is needed to guarantee it *can* commit later — validates constraints, acquires locks, writes an undo/redo log record — then replies `VOTE-COMMIT` or `VOTE-ABORT`, and durably records that vote before sending it. This is the crucial detail: once a participant votes commit, it has entered an **uncertain state** where it must be able to honor that vote no matter what happens next, including its own crash and restart.

**Phase 2 — Commit/Abort.** If the coordinator receives `VOTE-COMMIT` from everyone, it durably logs the decision and sends `GLOBAL-COMMIT`; if any participant votes abort (or times out), it sends `GLOBAL-ABORT`. Participants apply the decision, release locks, and acknowledge.

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

## Why 2PC blocks

Look at participant `P` sitting in `READY` (it voted commit, holding locks, waiting for the coordinator's final word) when the coordinator crashes before phase 2 goes out. `P` cannot unilaterally commit — some other participant might have voted abort and never told `P`. `P` cannot unilaterally abort either — some other participant might already have received `GLOBAL-COMMIT` and applied it. `P` has no safe default; the only correct move is to keep waiting, keep the locks held, and keep every other transaction that needs those locks waiting too. Asking surviving participants doesn't help in general, since in the worst case they're all in `READY` with no more information than `P` has. This is exactly the failure Gray & Lamport's paper formalizes: 2PC is a **blocking protocol** — a single coordinator crash at the wrong instant can stall participants indefinitely, and the only real remedy is a human operator or a recovery coordinator with out-of-band knowledge of the outcome.

## Three-phase commit

Dale Skeen's 1981 paper introduces 3PC by inserting a **pre-commit** phase between voting and committing, so that no participant ever has to guess the outcome from an ambiguous state:

1. **CanCommit** — same as 2PC's prepare: coordinator asks, participants vote.
2. **PreCommit** — if all votes are commit, the coordinator broadcasts `PRE-COMMIT` and waits for acks. A participant that reaches `PRE-COMMIT` now knows *every other participant voted commit too* — the outcome is settled, only the final signal is missing.
3. **DoCommit** — once all acks are in, the coordinator sends `DO-COMMIT`.

The payoff: if the coordinator dies with some participant already in `PRE-COMMIT`, the surviving participants can safely elect a new coordinator and it can move the transaction forward to commit, because reaching `PRE-COMMIT` is proof the whole cohort agreed. Nobody who has *not* seen `PRE-COMMIT` will have committed either, so aborting is also always safe for a node that never got that far. That extra round trip is what buys non-blocking behavior — but only under Skeen's assumed failure model: **crash-stop failures and no network partitions**. The recovery coordinator has to be able to reach a live majority to determine state, and it has to be sure that a node it can't reach really has crashed rather than being cut off.

That second assumption is where 3PC falls apart in practice. If the network partitions instead of a node cleanly crashing, one side of the partition can see enough participants to conclude "everyone was in PRE-COMMIT, safe to commit" while the coordinator (or a competing recovery coordinator) on the other side reaches the opposite conclusion and aborts — a split-brain commit/abort disagreement, the exact inconsistency atomic commit was supposed to prevent. Van Steen & Tanenbaum's treatment of this is blunt: 3PC solves blocking under a failure model that excludes the failure mode — partitions — that real distributed systems have to survive most often.

## 2PC vs 3PC at a glance

| | 2PC | 3PC |
|---|---|---|
| Phases | Prepare, Commit | CanCommit, PreCommit, DoCommit |
| Coordinator crash, no partition | Participants in READY block indefinitely | Recovery coordinator can resolve via PRE-COMMIT state |
| Network partition | Blocks (same as coordinator crash) | Can produce inconsistent commit/abort across partitions |
| Message rounds | 2 | 3 |
| Assumed failure model | Crash-stop | Crash-stop, **no partitions** |
| Extra durable log writes per participant | 1 (vote) | 2 (vote, pre-commit ack) |
| Production adoption | XA/JTA, distributed SQL commit paths | Rare — mostly academic and internal research systems |

## Why consensus won instead

The actual fix is not a third phase — it's replacing a single fallible coordinator with a **replicated** decision. Gray & Lamport's paper shows that running the commit *decision itself* through Paxos (their "Paxos Commit") tolerates coordinator failures without blocking, because no single node's crash can leave the outcome undetermined: a majority of acceptors durably records the decision, and any live majority can recover it. Raft-based systems get the same property by making the transaction log itself a replicated, majority-committed log rather than a promise held by one process. This is also the reasoning behind the "sagas over two-phase commit" approach for microservices: if you can't afford the coordination cost of a consensus-backed commit, don't approximate atomicity with a protocol that blocks — decompose into local transactions with compensations instead. Atomic commit across nodes is now mostly built on top of consensus (commit records replicated via Raft/Paxos-based logs), not the bare 2PC/3PC handshake, precisely because "blocking under partition" is disqualifying for anything that has to stay available.

**Try next:** implement the 2PC state machine above with an artificial coordinator crash injected right after collecting all `VOTE-COMMIT` responses but before sending `GLOBAL-COMMIT`, then watch your participant sit in `READY` — measure how long it holds its locks before you add any recovery logic at all.
