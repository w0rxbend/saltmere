---
title: "FLP: why no async consensus protocol can be both safe and guaranteed to terminate"
date: 2026-07-31
track: distributed-systems
summary: "The Fischer–Lynch–Paterson result proves that in a purely asynchronous system, no deterministic protocol solves consensus if even one process may crash. It doesn't say consensus is impossible in practice — it says exactly which assumption every working system quietly adds to escape it."
reading_time: 5
tags: [flp, consensus, impossibility, asynchrony, failure-detectors, partial-synchrony]
sources:
  - title: "Fischer, Lynch, Paterson — Impossibility of Distributed Consensus with One Faulty Process (JACM 1985)"
    url: "https://groups.csail.mit.edu/tds/papers/Lynch/jacm85.pdf"
  - title: "Journal of the ACM entry (10.1145/3149.214121)"
    url: "https://dl.acm.org/doi/10.1145/3149.214121"
  - title: "2001 PODC Influential Paper (Dijkstra) Award citation"
    url: "https://www.podc.org/influential/2001-influential-paper/"
  - title: "Henry Robinson — A Brief Tour of FLP Impossibility"
    url: "https://www.the-paper-trail.org/post/2008-08-13-a-brief-tour-of-flp-impossibility/"
  - title: "Chandra, Toueg — Unreliable Failure Detectors for Reliable Distributed Systems (JACM 1996)"
    url: "https://dl.acm.org/doi/10.1145/226643.226647"
---

Every consensus system you use — Raft, Paxos, ZooKeeper, etcd — works in practice. The FLP theorem says every one of them is, strictly speaking, capable of never terminating. Understanding *why* that is true, and why it doesn't matter operationally, is one of the highest-leverage pieces of theory in distributed systems, because it tells you precisely which knob every real system turns to make progress.

## What the theorem actually claims

Fischer, Lynch and Paterson (JACM, 1985) proved: **in an asynchronous message-passing system, there is no deterministic protocol that solves consensus if even a single process may fail by crashing.**

Pin down the words, because each one is load-bearing:

- **Consensus** means all correct processes must (a) agree on the same value, (b) only ever decide a value some process proposed, and (c) *eventually decide* — termination.
- **Asynchronous** means there is no bound on message delay or relative process speed. A slow message and a crashed sender are indistinguishable: you can wait, but you can never conclude "it's dead."
- **Deterministic** means no coin flips. Given the same state and messages, a process always does the same thing.
- **One faulty process** — just one possible crash. The result is not about hostile majorities; a single crash is enough.

The impossibility is about the *combination*. Drop asynchrony, allow randomness, or forbid all failures, and consensus becomes solvable. FLP says you cannot have deterministic, always-terminating, crash-tolerant consensus over a fully asynchronous network. Safety and liveness cannot both be guaranteed unconditionally.

## The shape of the proof: bivalence

You don't need the full proof, but the mechanism is worth carrying around. Call a reachable configuration (global state) **bivalent** if, depending on what happens next, the system could still decide either 0 or 1. Call it **univalent** once the outcome is fixed.

The proof has three moves:

1. **There is a bivalent initial configuration.** With processes proposing different values and one possibly crashing, you can always find a starting state whose outcome isn't yet determined.
2. **From any bivalent configuration you can stay bivalent.** For any single pending message, the adversary (the scheduler) can order delivery so the system reaches another bivalent configuration rather than committing to a value. This is the technical core: the "critical" message that would force a decision can always be delayed just past the step that would have forced it.
3. **Therefore there is an infinite bivalent run.** The scheduler keeps the system perpetually undecided by delaying one message at a time. No crash ever happens — the processes are simply never allowed to conclude anyone has crashed.

The adversary here isn't malice; it's an unlucky-but-legal ordering of messages the async model permits. The system never *has* to hang — it just can't rule the hang out.

## How every real system escapes it

Nobody repealed FLP; they sidestepped it by adding exactly one assumption:

```text
Randomization     -> break determinism.
                     Ben-Or-style protocols flip coins; they terminate
                     with probability 1 (expected finite rounds).

Partial synchrony -> assume timing bounds hold *eventually*.
                     Dwork-Lynch-Stockmeyer: after some unknown GST
                     ("global stabilization time") the network behaves.
                     Raft's election timeouts, Paxos leaders live here.

Failure detectors -> assume an oracle that's eventually accurate.
                     Chandra-Toueg's <>S is the *weakest* detector
                     that makes consensus solvable.
```

Raft is the cleanest illustration. Its randomized election timeout is a coin flip (dodging determinism) *and* a bet that the network is synchronous enough for one candidate to win before the next timeout (partial synchrony). When two candidates keep splitting the vote, Raft genuinely fails to make progress — that livelock **is** FLP showing through — and the randomized backoff is what makes the stall almost-surely temporary rather than permanent.

## Why this changes how you read a design

Once you internalize FLP, you read consensus systems differently. You stop asking "does it always terminate?" (nothing does) and start asking "*what does it assume about timing, and what happens to liveness when that assumption is violated?*" A partition that breaks the synchrony bet doesn't corrupt Raft — safety holds unconditionally — it just stops the cluster from committing until the network heals. That is the correct, and only available, trade: keep safety always, and make liveness conditional on the network eventually behaving.

**Try next:** run a 3-node etcd or a Raft toy and use `iptables`/`tc` to inject asymmetric delays that keep triggering split-vote elections. Watch the cluster refuse to elect a leader while every node stays perfectly consistent. You are watching a live FLP scenario: safety preserved, termination denied, exactly until you let the timing assumption hold again.
