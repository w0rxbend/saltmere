---
title: "FLP: why no asynchronous consensus protocol is both safe and guaranteed to terminate"
date: 2026-07-31
track: distributed-systems
summary: "The Fischer–Lynch–Paterson result proves that in a purely asynchronous system, no deterministic protocol solves consensus if even one process may crash. It does not say consensus is impossible in practice — it identifies the assumption every working system adds to escape it."
reading_time: 6
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

**Gist.** Consensus requires processes to agree on one proposed value and to decide eventually, but in an asynchronous network a crashed process and a slow one are indistinguishable. Fischer, Lynch and Paterson (JACM, 1985) proved that **no deterministic protocol achieves all three consensus properties when even a single process may crash**, because an adversarial message schedule can hold the system in a perpetually undecided state. Every deployed system escapes the result by weakening one premise — determinism, asynchrony, or the absence of failure information — and the cost is that **termination becomes conditional while agreement stays unconditional**.

## What the theorem claims

The statement is: in an asynchronous message-passing system, there is no deterministic protocol that solves consensus if even one process may fail by crashing. Each term is load-bearing.

- **Consensus** requires three properties together: *agreement*, all correct processes decide the same value; *non-triviality* (the paper's form of validity), both 0 and 1 are the decision value of some run, which rules out a protocol that always decides a constant; and *termination*, every correct process eventually decides.
- **Asynchronous** means no bound exists on message delay or on relative process speed. A message delayed arbitrarily long and a sender that has crashed produce the same observation at the receiver, so **no amount of waiting converts silence into evidence of failure**.
- **Deterministic** means the protocol has no source of randomness: the next step of a process is a function of its state and the message it receives.
- **One faulty process** is the failure budget. The result does not depend on hostile or Byzantine behaviour, nor on a majority failing; a single crash-stop failure suffices.

The impossibility is a property of the combination. Removing asynchrony, admitting randomness, or forbidding failures altogether each makes consensus solvable. What cannot exist is a protocol that is simultaneously deterministic, crash-tolerant, always-terminating and correct over a fully asynchronous network. **Safety and liveness cannot both hold unconditionally.**

## The proof mechanism: bivalence

Call a reachable global state — the states of all processes plus the set of messages in transit — a *configuration*. A configuration is **bivalent** if runs extending it exist that decide 0 and other runs that decide 1; it is **univalent** once every extension decides the same value. Non-triviality forces the initial configurations to include both possible outcomes, and agreement forces every decided configuration to be univalent, so any terminating run must cross from bivalent to univalent at some step.

The argument has three moves.

1. **A bivalent initial configuration exists.** Order the initial configurations so that adjacent ones differ in a single process's proposal. If all were univalent, some adjacent pair would differ in valency; the crash of the one process that distinguishes them makes the two runs indistinguishable to everybody else, so both must decide the same value — a contradiction. Hence some initial configuration is bivalent.
2. **Bivalence can always be preserved for one more step.** For a bivalent configuration and any pending message, the paper shows that **some schedule can be run first so that delivering that message still leaves the system bivalent** — which is what lets the adversary delay a message without ever dropping it. The critical step that would fix the outcome depends on a particular message being delivered before another; the adversary reorders those two deliveries. Because the two steps act on different processes, they commute, and the process whose decision would have been forced can instead be treated as crashed for the remainder of the argument.
3. **Therefore an infinite non-deciding run exists.** Applying move 2 repeatedly while still delivering every message eventually produces a run that is admissible under the asynchronous model, involves at most one failure, and never reaches a univalent configuration.

The adversary is not malice; it is **an unlucky but legal interleaving of message deliveries that the asynchronous model permits**. A real system need not hang. It cannot rule the hang out.

### Implementation sketch (Scala)

The scheduler side of move 2 is what makes the theorem concrete: given a configuration and its pending messages, the adversary searches for a successor that is still bivalent. The valency test below is not computable in general — it quantifies over all extensions — which is exactly why this is a proof device rather than an algorithm.

```scala
final case class ProcState(decided: Option[Int], local: Vector[Byte])
final case class Message(to: Int, payload: Array[Byte])
final case class Config(states: Map[Int, ProcState], inFlight: List[Message])

// The protocol's transition function: deterministic in (state, message),
// which is the hypothesis FLP contradicts.
def step(s: ProcState, m: Message): (ProcState, List[Message]) = ???

def deliver(c: Config, m: Message): Config =
  val (next, sent) = step(c.states(m.to), m)
  // `eq` removes this delivery only, not an equal message queued elsewhere.
  Config(c.states.updated(m.to, next), c.inFlight.filterNot(_ eq m) ++ sent)

enum Valency:
  case Zero, One, Bi

// Not implementable: ranges over every admissible extension of `c`.
def valency(c: Config): Valency = ???

/** Move 2, reduced to its one-step case: the adversary looks for a
  * delivery that keeps `c` bivalent. Every candidate is a legal
  * asynchronous schedule, so the run it builds is admissible. */
def stayBivalent(c: Config): Option[Config] =
  c.inFlight.iterator.map(deliver(c, _)).find(valency(_) == Valency.Bi)

/** The run move 3 builds: a stream in which no configuration is ever
  * univalent. Fairness — every message eventually delivered — is a
  * separate obligation the proof discharges and this sketch does not. */
def undecidedRun(c0: Config): LazyList[Config] =
  LazyList.unfold(c0)(c => stayBivalent(c).map(next => (next, next)))
```

## How deployed systems escape the result

No system repeals FLP; each adds one assumption that falsifies a premise.

```text
Randomization     -> break determinism.
                     Ben-Or-style protocols flip coins; they terminate
                     with probability 1 (expected finite rounds).

Partial synchrony -> assume timing bounds hold *eventually*.
                     Dwork-Lynch-Stockmeyer: after some unknown GST
                     ("global stabilization time") the network behaves.
                     Raft's election timeouts, Paxos leaders live here.

Failure detectors -> assume an oracle that is eventually accurate.
                     Chandra-Toueg's <>S solves consensus provided a
                     majority of processes are correct; <>W, the
                     weakest class they define, is equivalent to it.
```

Raft combines two of them. Its **randomized election timeout breaks determinism**, and the timeout value itself is a bet that the network is synchronous enough for one candidate to complete an election before the next timeout fires — partial synchrony. When candidates repeatedly split the vote, Raft makes no progress, and **that livelock is the FLP scenario appearing in production**: the randomized backoff makes the stall almost surely temporary rather than permanent, but does not bound it.

## Reading a design through the theorem

The question "does this protocol always terminate?" has a fixed answer for every asynchronous crash-tolerant protocol, so it carries no information. The useful questions are **what the protocol assumes about timing** and **what happens to liveness once that assumption is violated**. A partition that breaks the synchrony bet does not corrupt Raft — agreement holds unconditionally — it prevents commits until the network recovers. That division is the only trade the theorem leaves available: **safety always, liveness conditional on the network eventually behaving**.

An observable demonstration: run a three-node etcd or a Raft implementation and use `iptables` or `tc` to inject asymmetric delays that keep triggering split-vote elections. The cluster fails to elect a leader while every node remains consistent — safety preserved, termination denied, until the timing assumption is allowed to hold again.

## Pitfalls

- **Reading FLP as "consensus is impossible."** The theorem denies a guarantee of termination for deterministic protocols under asynchrony; it says nothing against protocols that terminate with probability 1 or terminate after an unknown stabilisation time.
- **Treating an election timeout as a failure detector.** A timeout firing means no message arrived within the bound, not that the peer crashed; under sufficient delay the timeout fires against a live leader and triggers an unnecessary election.
- **Tuning election timeouts downward to "improve availability."** Shorter timeouts increase the rate of spurious elections and split votes, so the cluster spends more time without a leader.
- **Assuming randomised backoff bounds the stall.** Termination with probability 1 gives an expected number of rounds, not a worst-case one; a run of unlucky schedules is a legal outcome, not a bug to be found in the implementation.
- **Blaming a stalled cluster on the consensus implementation.** Absence of commits with no divergence between replicas is the expected behaviour when the timing assumption fails; the defect, if any, is in the network path rather than the protocol.
- **Expecting a Byzantine-tolerant protocol to sidestep the result.** FLP already holds with a single crash failure, so strengthening the fault model cannot recover guaranteed termination under asynchrony.
