---
title: "Viewstamped Replication: Consensus Without Paxos"
date: 2026-07-30
track: distributed-systems
summary: "VSR reaches consensus by replicating a log under a primary that changes by view number. A walk through its three sub-protocols — normal operation, view change, recovery — and why it reads like Raft's older sibling."
reading_time: 5
tags: [consensus, replication, vsr, distributed-systems, raft, paxos]
sources:
  - title: "Oki & Liskov, Viewstamped Replication (PODC 1988)"
    url: "http://www.pmg.csail.mit.edu/vr/oki88vr-abstract.html"
  - title: "Liskov & Cowling, Viewstamped Replication Revisited (MIT-CSAIL-TR-2012-021)"
    url: "http://pmg.csail.mit.edu/papers/vr-revisited.pdf"
  - title: "The Morning Paper: Viewstamped Replication Revisited"
    url: "https://blog.acolyer.org/2015/03/06/viewstamped-replication-revisited/"
  - title: "TigerBeetle: Viewstamped Replication Made Famous"
    url: "https://github.com/tigerbeetle/viewstamped-replication-made-famous"
---

You want a replicated state machine that survives crashes: give it the same commands in the same order on every node, and each node ends up in the same state. The hard part is agreeing on that order while machines fail and recover. Paxos solves it and is famously hard to hold in your head. Raft solves it and is deliberately teachable. **Viewstamped Replication (VSR) solves it too — and it did so in 1988**, a decade before Paxos was published, framed not as an abstract consensus box but as a way to replicate a service.

That framing is why VSR is pleasant to implement. There is no separate "consensus module" bolted onto a log. VSR *is* a replicated log driven by a **primary** and a **view number**, and the whole protocol is three sub-protocols that hand control between each other. If you have read Raft, most of this will feel like déjà vu — for good reason.

## The state each replica keeps

Following *Viewstamped Replication Revisited* (Liskov & Cowling, 2012), a group has `2f + 1` replicas and tolerates `f` failures. Every quorum is `f + 1`, so any two quorums overlap in at least one replica — that intersection is what makes the protocol safe. Each replica holds:

- **configuration**: sorted array of the `2f + 1` replica addresses
- **replica number**: this node's index into that array
- **view-number**: which view we are in, initially 0
- **status**: `normal`, `view-change`, or `recovering`
- **op-number**: the number of the most recently received request
- **log**: the ordered list of operations
- **commit-number**: op-number of the most recently committed operation
- **client-table**: the latest request seen per client (for exactly-once semantics)

The primary is not elected by a vote. It is **computed**: `primary = view-number mod N`. Everyone who agrees on the view number agrees on who the primary is, for free. Advancing leadership means advancing the view number — hence "viewstamped."

## Sub-protocol 1: normal operation

While the primary is alive, this is the entire hot path. No disk-forced elections, just log replication.

```text
client  -> primary : REQUEST(op, client-id, request-num)

primary:
  advance op-number
  append op to log
  update client-table[client-id]
  send PREPARE(view, op, op-number, commit-number) to all backups

backup (in status=normal, matching view):
  # only accept if op-number is exactly next; otherwise do state transfer
  append op to log
  update client-table
  send PREPAREOK(view, op-number) to primary

primary:
  on f PREPAREOK for op-number (f+1 total incl. itself):
    commit-number = op-number
    result = execute(op)                 # up-call to the service
    send REPLY(view, request-num, result) to client
```

Commit is piggybacked: backups learn an operation committed from the `commit-number` on the *next* PREPARE. When the primary is idle it sends an explicit **COMMIT** message so backups do not lag. That is the whole steady state — one round trip to a quorum, then execute.

## Sub-protocol 2: view change

Backups run a timer on messages from the primary. Silence past the timeout means "assume the primary is dead, move to the next view."

1. A suspicious backup increments its view-number, sets status to `view-change`, and broadcasts **STARTVIEWCHANGE(v)**.
2. When a replica has `f` STARTVIEWCHANGE messages for view `v` (a quorum with itself), it sends **DOVIEWCHANGE(v, log, last-normal-view, op-number, commit-number)** to the *new* primary — the node where `v mod N` points.
3. The new primary waits for `f + 1` DOVIEWCHANGE messages. It picks the **most up-to-date log** among them (largest `last-normal-view`, then largest op-number), sets its own state from it, and broadcasts **STARTVIEW(v, log, op-number, commit-number)**.
4. Backups install that log, set status back to `normal`, and resume.

The quorum in step 3 is the safety linchpin. Any committed operation reached `f + 1` logs during normal operation; any view-change quorum of `f + 1` must intersect that set, so the winning log necessarily contains every committed operation. Nothing acknowledged to a client can be lost.

## Sub-protocol 3: recovery

A replica that crashed must not rejoin claiming state it no longer has — a stale vote could violate a quorum. VR Revisited's key move is that a recovering node treats itself as **untrustworthy** until it is caught up.

```text
recovering:
  status = recovering
  pick a fresh nonce x
  send RECOVERY(i, x) to all

replica (status=normal):
  send RECOVERYRESPONSE(view, x, log?, op-number?, commit-number?, j)
  # only the current primary includes its log/op/commit-number

recovering:
  wait for f+1 RECOVERYRESPONSE with matching nonce x,
    including one from the primary of the latest view
  adopt that state, status = normal
```

The **nonce** ties responses to *this* recovery attempt, so a slow reply from an earlier crash cannot be mistaken for a current one. Note VSR's original design assumes crash-recovery and does not, by itself, require disk writes on the hot path — state can be reconstructed from a quorum. TigerBeetle's production VSR pushes further, adding storage-fault tolerance on top.

## How it relates to Paxos and Raft

| | VSR | Multi-Paxos | Raft |
|---|---|---|---|
| Leadership | primary = `view mod N` | distinguished proposer | elected leader |
| Epoch counter | view-number | ballot/proposal number | term |
| Leader on failure | round-robin, deterministic | proposer races | randomized-timeout election |
| Mental model | replicated log/service | agreement on a value | replicated log |

The mapping is almost mechanical: VSR's **view** is Raft's **term**, VSR's **primary** is Raft's **leader**, VSR's **view change** is Raft's **leader election**. The substantive difference is leader selection — VSR rotates the primary by a deterministic function of the view number, so there is no candidate race, while Raft holds a randomized election. Paxos, by contrast, agrees on values and leaves leadership and log management to a "Multi-Paxos" layer that VSR and Raft build in from the start. VSR predating Paxos is why people call these two the same idea discovered twice.

**Try next:** Implement normal operation for a 3-node group (`f = 1`) with in-process message passing, then kill the primary mid-request and code the view change until a backup at `view mod N` takes over and replays the winning log.
