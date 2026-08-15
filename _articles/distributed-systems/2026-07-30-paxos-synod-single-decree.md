---
title: "Single-Decree Paxos: How a Majority Agrees on One Value"
date: 2026-07-30
track: distributed-systems
summary: "The Synod protocol at the heart of Paxos: two phases, proposal numbers, and the majority-quorum overlap that makes a single chosen value stick, even when proposers race and messages drop."
reading_time: 7
tags: [paxos, synod, consensus, quorum, proposers-acceptors, raft]
sources:
  - title: "The Part-Time Parliament (ACM TOCS 1998) — Leslie Lamport"
    url: "https://lamport.azurewebsites.net/pubs/lamport-paxos.pdf"
  - title: "Paxos Made Simple (2001) — Leslie Lamport"
    url: "https://lamport.azurewebsites.net/pubs/paxos-simple.pdf"
  - title: "Single-Decree Paxos — Michael Whittaker"
    url: "https://mwhittaker.github.io/blog/single_decree_paxos/"
  - title: "In Search of an Understandable Consensus Algorithm (Raft) — Ongaro & Ousterhout"
    url: "https://raft.github.io/"
  - title: "Paxos vs Raft: Have we reached consensus on distributed consensus? — Heidi Howard & Richard Mortier"
    url: "https://dl.acm.org/doi/10.1145/3380787.3393681"
---

**Gist.** Consensus is the problem of making a set of processes agree on a single value despite crashes, message loss and reordering. Single-decree Paxos — the *Synod* protocol of Lamport's "The Part-Time Parliament" — solves one instance of that problem with two round-trip phases over a majority of acceptors, where the second phase forces a proposer to adopt any value a previous proposer may already have had chosen. The cost is two network round trips per decision, durable storage on every acceptor before every reply, and no liveness guarantee: two proposers can out-number each other indefinitely.

Multi-Paxos and Raft build a whole replicated *log* out of repeated agreement; the Synod is the atom they are made of.

## The three roles

- **Proposers** suggest values.
- **Acceptors** vote. They are the fault-tolerant memory of the system; a value is *chosen* when a majority of acceptors have accepted it.
- **Learners** discover which value was chosen and act on it.

A single physical node commonly plays all three roles. The acceptor logic is where safety lives, and it is the part that must be reasoned about exactly.

## Proposal numbers

Every proposal carries a globally unique, monotonically increasing **proposal number** `n`. Proposal numbers are not values; they are an ordering device. Uniqueness follows from partitioning the number space among proposers, for example `round * num_proposers + proposer_id`. A higher `n` supersedes a lower one, which is what allows a later proposer to take over from a stalled earlier one without endangering a value that has already been chosen.

## The two phases

**Phase 1 — Prepare / Promise.** A proposer picks a fresh `n` and sends `prepare(n)` to a majority of acceptors. An acceptor that has not already promised a strictly higher number replies with a **promise** never again to accept a proposal numbered below `n`, and — the load-bearing part — **reports the highest-numbered proposal `(n_accepted, v_accepted)` it has already accepted**, if any.

**Phase 2 — Accept / Accepted.** Having collected promises from a majority, the proposer selects the value to propose under a rule that is the whole of Paxos's safety argument:

- If **any** promise carried an already-accepted value, the proposer **must** propose the value attached to the **highest `n_accepted`** among the promises received. Its own preferred value is discarded.
- If no promise carried a value, the proposer may propose its own.

The proposer then sends `accept(n, v)` to a majority. An acceptor accepts unless it has since promised a number greater than `n`. Once a majority has accepted `(n, v)`, `v` is **chosen** — a state no single participant necessarily observes at the moment it occurs.

## Why a majority quorum is safe

Both phases require a **majority**. Any two majorities of the same acceptor set intersect in at least one member, by the pigeonhole principle. That overlapping acceptor is the linchpin: it cannot forget what it accepted, so a later proposer's Phase 1 is guaranteed to *see* an already-chosen value in at least one promise, and the Phase 2 rule then compels it to re-propose that value.

Lamport states the resulting invariant as **P2c**:

> "For any v and n, if a proposal with value v and number n is issued, then there is a set S consisting of a majority of acceptors such that either (a) no acceptor in S has accepted any proposal numbered less than n, or (b) v is the value of the highest-numbered proposal among all proposals numbered less than n accepted by the acceptors in S."

The consequence: **once a value is chosen, every higher-numbered proposal subsequently issued carries that same value.** Agreement is preserved not by locking but by forcing new proposers to defer to what the quorum already remembers.

Two persistence requirements follow directly. The promise must reach stable storage *before* the promise reply is sent, and the accepted pair must reach stable storage *before* the accepted reply is sent. An acceptor that replies first and crashes before the write may, on recovery, contradict its own earlier message, which destroys the intersection argument.

## A walk-through

Five acceptors `{a, b, c, d, e}`; a majority is 3.

1. Proposer **X** runs `prepare(1)` and gets promises from `{a, b, c}`. None has accepted anything, so X proposes freely. X sends `accept(1, "apple")` to `{a, b, c}`, but only `a` and `b` receive it before X crashes. `"apple"` is accepted by two acceptors: **not** a majority, therefore **not chosen**.
2. Proposer **Y** runs `prepare(2)`, reaching `{c, d, e}`. None of `c`, `d`, `e` has accepted anything, so Y sees no prior value and proposes freely: `accept(2, "banana")` to `{c, d, e}`. All three accept. `"banana"` is chosen.
3. X recovers and retries `accept(1, "apple")` at `c`. Acceptor `c` promised `2 > 1` in step 2, so it **rejects**. `"apple"` can never reach a majority. Exactly one value was ever chosen.

The other ordering shows the override rule biting:

1. X gets `"apple"` accepted by the majority `{a, b, c}` under `n = 1`. `"apple"` is chosen.
2. Y runs `prepare(2)` to `{c, d, e}`. Acceptor `c` replies with `(1, "apple")`. Because at least one promise carried a value, Y is **forced** to propose `"apple"` rather than its own value, and sends `accept(2, "apple")`. The chosen value is preserved.

Note that in the second ordering Y never learns that `"apple"` was already chosen. The rule is applied unconditionally, because a proposer cannot distinguish "a value was chosen" from "a value was accepted by a minority".

### Implementation sketch (Scala)

The acceptor is two handlers over three persistent fields. The persistence call is placed before each reply deliberately; removing it breaks the quorum-overlap argument.

```scala
type Num = Long

enum Reply:
  case Promise(n: Num, acceptedN: Option[Num], acceptedV: Option[String])
  case Accepted(n: Num, v: String)
  case Nack(promised: Num)

final class Acceptor(persist: () => Unit):
  private var promisedN: Option[Num] = None
  private var acceptedN: Option[Num] = None
  private var acceptedV: Option[String] = None

  def onPrepare(n: Num): Reply =
    if promisedN.forall(n > _) then
      promisedN = Some(n)
      persist()                        // must reach stable storage before replying
      Reply.Promise(n, acceptedN, acceptedV)
    else Reply.Nack(promisedN.get)

  def onAccept(n: Num, v: String): Reply =
    if promisedN.forall(n >= _) then
      promisedN = Some(n); acceptedN = Some(n); acceptedV = Some(v)
      persist()
      Reply.Accepted(n, v)
    else Reply.Nack(promisedN.get)

/** Phase 2 value selection: adopt the value of the highest-numbered
  * accepted proposal reported by the promise quorum, else propose own. */
def chooseValue(promises: Seq[Reply.Promise], own: String): String =
  promises
    .flatMap(p => p.acceptedN.zip(p.acceptedV))
    .maxByOption(_._1)
    .map(_._2)
    .getOrElse(own)
```

A proposer gathers a majority of promises, applies `chooseValue`, then sends `accept`. On a `Nack` it raises `n` above the reported `promised` and restarts from Phase 1.

## How it differs from Raft

Raft solves the same underlying problem with different engineering commitments.

| | Single-decree Paxos | Raft |
|---|---|---|
| Scope | one value | a replicated log |
| Leadership | none required; any proposer may propose | a single elected leader drives all writes |
| Concurrency | competing proposers are normal, and can livelock | one leader at a time by design |
| Stated design goal | minimal, provable safety core | understandability, the explicit thesis of its paper |
| Log holes | not applicable (single value) | disallowed; the log must stay contiguous |

Raft's strong leader and contiguous-log constraint mean the common case has no competing proposers and no per-slot value-selection reasoning. Raw single-decree Paxos is rarely deployed unmodified; deployments use Multi-Paxos with a distinguished proposer, which avoids — in the common case, though not by construction — the livelock two proposers create by repeatedly out-numbering each other in Phase 1. Howard and Mortier compare the two algorithms and conclude that they are more alike than usually assumed, with the substantive differences concentrated in leader election rather than in the consensus core.

## Pitfalls

- **Replying before the write lands.** An acceptor that sends a promise or an accepted reply before the corresponding state is durable can, after a crash and restart, accept a lower-numbered proposal it already promised to refuse; two different values then reach majorities.
- **A proposer using its own value after seeing a non-empty promise.** The result is two chosen values, and the violation is invisible on the happy path because it only manifests when a prior minority accept existed.
- **Picking the wrong promise.** Phase 2 must adopt the value with the highest `n_accepted` across the quorum, not the first non-empty reply, not the most frequent value, and not the highest promise number.
- **Non-unique proposal numbers.** Two proposers issuing the same `n` with different values can both pass the acceptor's `n >= promisedN` test, so both values can be accepted under the same number.
- **Waiting for all acceptors instead of a majority.** Correctness requires a majority; requiring more than that trades away exactly the fault tolerance the protocol was built for, and a single unreachable acceptor stalls every decision.
- **Duelling proposers.** Each proposer's Phase 1 invalidates the other's Phase 2, so the protocol makes no progress while both retry with increasing numbers; Paxos guarantees safety, not liveness.
- **Assuming a proposer knows a value was chosen.** A successful `accept` from a majority chooses the value, but no participant necessarily observes the majority; learning the outcome is a separate step.
