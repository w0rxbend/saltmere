---
title: "Viewstamped Replication: Consensus Without Paxos"
date: 2026-07-30
track: distributed-systems
summary: "Viewstamped Replication reaches consensus by replicating a log under a primary that changes by view number. A walk through its three sub-protocols — normal operation, view change, recovery — and its correspondence with Raft."
reading_time: 7
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

**Gist.** A replicated state machine gives every node the same commands in the same order, but agreeing on that order while machines crash and recover is the hard part. Viewstamped Replication (VSR) solves it with a **replicated log driven by a primary whose identity is a function of a monotonically increasing view number**, split into three sub-protocols: normal operation, view change, and recovery. The cost is that every request pays a round trip to a quorum of `f + 1` replicas before it can be executed, and any primary failure stalls the group until a view change completes.

VSR was published by Oki and Liskov in 1988 (PODC), a decade before Paxos appeared in print, and was framed not as an abstract agreement box but as a method for replicating a service. The 2012 revision, *Viewstamped Replication Revisited* (Liskov & Cowling, MIT-CSAIL-TR-2012-021), restates the protocol in the form described below and is the reference for the state and message names used here.

## The state each replica keeps

A group has `2f + 1` replicas and tolerates `f` failures. Every quorum is `f + 1`, so **any two quorums share at least one replica** — that intersection is the entire safety argument. Each replica holds:

- **configuration**: sorted array of the `2f + 1` replica addresses
- **replica number**: this node's index into that array
- **view-number**: the current view, initially 0
- **status**: `normal`, `view-change`, or `recovering`
- **op-number**: the number of the most recently received request
- **log**: the ordered list of operations
- **commit-number**: op-number of the most recently committed operation
- **client-table**: the latest request seen per client, which is what makes repeated client requests execute at most once

The primary is not elected by a vote. It is **computed as `primary = view-number mod N`**. Any two replicas that agree on the view number agree on the primary without exchanging a message. Advancing leadership therefore means advancing the view number. The name comes from the 1988 paper, in which each operation carries a *viewstamp* pairing the view number with the operation's position within that view.

## Sub-protocol 1: normal operation

While the primary is alive this is the entire hot path.

```text
client  -> primary : REQUEST(op, client-id, request-num)

primary:
  advance op-number
  append op to log
  update client-table[client-id]
  send PREPARE(view, op, op-number, commit-number) to all backups

backup (in status=normal, matching view):
  # accept only if op-number is exactly the next one; otherwise state transfer
  append op to log
  update client-table
  send PREPAREOK(view, op-number) to primary

primary:
  on f PREPAREOK for op-number (f+1 total including itself):
    commit-number = op-number
    result = execute(op)                 # up-call to the service
    send REPLY(view, request-num, result) to client
```

Two guards carry the weight. A backup **rejects a PREPARE whose view differs from its own**, which prevents a deposed primary from extending logs in a view the group has left. A backup **rejects a PREPARE whose op-number is not exactly one past its own**, which keeps each log a prefix-consistent sequence rather than a set with holes; a gap forces state transfer instead.

Commit is piggybacked: backups learn that an operation committed from the `commit-number` carried on the *next* PREPARE. When the primary has no client traffic it sends an explicit **COMMIT** message, which both advances backups' commit-numbers and serves as the liveness signal their timers watch.

## Sub-protocol 2: view change

Backups run a timer on messages from the primary. Silence past the timeout is treated as primary failure.

1. A backup increments its view-number, sets status to `view-change`, and broadcasts **STARTVIEWCHANGE(v)**.
2. On receiving `f` STARTVIEWCHANGE messages for view `v` — a quorum counting itself — a replica sends **DOVIEWCHANGE(v, log, last-normal-view, op-number, commit-number)** to the new primary, the node at index `v mod N`.
3. The new primary waits for `f + 1` DOVIEWCHANGE messages, selects the **most up-to-date log among them — largest `last-normal-view` first, then largest op-number** — installs it, and broadcasts **STARTVIEW(v, log, op-number, commit-number)**.
4. Backups install that log, set status back to `normal`, and resume.

The safety argument is the quorum intersection. Any operation that committed did so because `f + 1` replicas held it in their logs. The `f + 1` DOVIEWCHANGE messages in step 3 must overlap that set in at least one replica, so **the winning log contains every operation ever acknowledged to a client**. Operations that were prepared but never committed may or may not survive, which is why a client must not treat a prepare as a reply.

The `last-normal-view` field is what makes the comparison correct: op-number alone is not a valid ordering, because a replica can carry a high op-number accumulated in a view that was later abandoned without those operations committing. The tie-break is lexicographic on `(last-normal-view, op-number)`.

## Sub-protocol 3: recovery

A replica that crashed must not rejoin claiming state it no longer holds, since a stale participant in a quorum can break the intersection argument. The revised protocol has the recovering node treat itself as **untrustworthy until it has been caught up by a quorum**.

```text
recovering:
  status = recovering
  pick a fresh nonce x
  send RECOVERY(i, x) to all

replica (status=normal):
  send RECOVERYRESPONSE(view, x, log?, op-number?, commit-number?, j)
  # only the current primary includes its log/op-number/commit-number

recovering:
  wait for f+1 RECOVERYRESPONSE with matching nonce x,
    including one from the primary of the latest view
  adopt that state, status = normal
```

The **nonce ties responses to this particular recovery attempt**, so a delayed response generated during an earlier crash cannot be mistaken for a current one. A recovering replica sends no PREPAREOK and no DOVIEWCHANGE, so it contributes to no quorum while its state is unknown. VSR's design assumes crash-recovery and, on its own, does not require a disk write on the request path — a recovering replica reconstructs state from a quorum rather than from local storage. TigerBeetle's production VSR implementation extends the protocol with storage-fault tolerance.

## Correspondence with Paxos and Raft

| | VSR | Multi-Paxos | Raft |
|---|---|---|---|
| Leadership | primary = `view mod N` | distinguished proposer | elected leader |
| Epoch counter | view-number | ballot/proposal number | term |
| Leader on failure | round-robin, deterministic | proposer races | randomized-timeout election |
| Mental model | replicated log/service | agreement on a value | replicated log |

The mapping is close to mechanical: VSR's **view** corresponds to Raft's **term**, VSR's **primary** to Raft's **leader**, VSR's **view change** to Raft's **leader election**. The substantive difference is leader selection. VSR rotates the primary by a deterministic function of the view number, so there is no candidate race, but a view whose designated primary is itself down must be abandoned in favour of the next view. Raft holds a randomized election, so any sufficiently up-to-date replica can win a term. Paxos agrees on values and leaves leadership and log management to a Multi-Paxos layer that both VSR and Raft include from the start.

### Implementation sketch (Scala)

The load-bearing part of a view change is the log-selection rule, not the messaging. The following models step 3.

```scala
final case class Entry(view: Int, op: String)

final case class DoViewChange(
    view: Int,
    log: Vector[Entry],
    lastNormalView: Int,
    opNumber: Int,
    commitNumber: Int
)

final class NewPrimary(f: Int, view: Int):
  private var pending: Map[Int, DoViewChange] = Map.empty

  /** Returns the state to broadcast in STARTVIEW once a quorum has replied. */
  def receive(from: Int, m: DoViewChange): Option[DoViewChange] =
    if m.view != view then None
    else
      pending = pending.updated(from, m)
      Option.when(pending.size >= f + 1):
        // (lastNormalView, opNumber) lexicographic: op-number alone would
        // prefer a log grown in a view that was abandoned before committing.
        val winner = pending.values.maxBy(d => (d.lastNormalView, d.opNumber))
        winner.copy(
          view = view,
          commitNumber = pending.values.map(_.commitNumber).max
        )
```

`maxBy` on a tuple gives the lexicographic order directly. The commit-number is taken as the maximum across the quorum: any replica in the quorum that observed a higher commit-number observed a real commit, and the winning log is a superset of everything committed.

## Pitfalls

- **Comparing DOVIEWCHANGE logs by op-number alone.** A replica that received many PREPAREs in a view that ended before those operations committed carries the highest op-number and the stalest useful state; selecting it discards committed operations from another replica. The comparison must lead with `last-normal-view`.
- **Accepting a PREPARE with a lower view number.** A partitioned primary from an old view continues sending PREPAREs; a backup that appends them corrupts its log with entries the current view never authorised.
- **Counting a recovering replica in a quorum.** A node in status `recovering` has unknown state; a PREPAREOK or DOVIEWCHANGE from it inflates a quorum that the intersection argument assumes is made of replicas with real logs.
- **Reusing a nonce across recovery attempts.** A RECOVERYRESPONSE generated for an earlier crash then satisfies the current attempt, and the recovering node adopts state from a view that has since been abandoned.
- **Waiting for `f + 1` PREPAREOK messages.** The primary counts its own append towards the quorum, so it needs `f` PREPAREOK messages from backups, not `f + 1`; requiring `f + 1` responses stalls a group of exactly `2f + 1` replicas whenever one backup is down.
- **Omitting the idle COMMIT message.** Without traffic there is no PREPARE to piggyback commit-numbers on, backups' primary timers expire, and the group runs a view change against a healthy primary.
