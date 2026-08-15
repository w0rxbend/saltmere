---
title: "Ben-Or's randomized consensus: a coin flip around FLP"
date: 2026-08-07
track: distributed-systems
summary: "FLP establishes that no deterministic asynchronous protocol guarantees consensus in the presence of a single crash. Ben-Or's 1983 protocol has every process flip a private fair coin when a round ends deadlocked. This covers the two-phase round, the exact thresholds, the requirement n > 2f, and the O(2^n) expected rounds that local coins impose."
reading_time: 7
tags: [consensus, randomization, flp, asynchrony, distributed-systems, byzantine-fault-tolerance]
sources:
  - title: "Ben-Or — Another Advantage of Free Choice: Completely Asynchronous Agreement Protocols (PODC 1983, hosted PDF)"
    url: "https://homepage.cs.uiowa.edu/~ghosh/BenOr.pdf"
  - title: "Aspnes — Randomized Protocols for Asynchronous Consensus (survey, arXiv cs/0209014)"
    url: "https://arxiv.org/pdf/cs/0209014"
  - title: "ETH Zürich DISCO — Distributed Systems lecture notes, Chapter 2 (Consensus / Ben-Or, Algorithm 2.15)"
    url: "https://disco.ethz.ch/courses/hs16/distsys/lnotes/chapter2_consensus_2on1.pdf"
  - title: "Murat Demirbas — The Ben-Or decentralized consensus algorithm"
    url: "http://muratbuffalo.blogspot.com/2019/12/the-ben-or-decentralized-consensus.html"
  - title: "Aguilera, Toueg — The correctness proof of Ben-Or's randomized consensus algorithm (hosted PDF)"
    url: "https://cs.nyu.edu/~apanda/classes/sp25/papers/aguilera-toueg10.pdf"
---

**Gist.** The Fischer–Lynch–Paterson (FLP) impossibility result states that in a fully asynchronous system with one possible crash failure, no deterministic protocol guarantees both agreement and termination, because the adversarial scheduler can always delay one critical message to keep the run undecided. Michael Ben-Or's 1983 protocol (*Another Advantage of Free Choice*, PODC) removes the scheduler's foresight: when a round ends without a majority, every process draws a fresh private fair coin, and the protocol terminates with probability 1. The cost is running time — with independent local coins, termination requires all coin-flipping processes to agree by chance, which takes **O(2^n) expected rounds**.

## The model and the resilience bound

The problem is binary consensus among `n` processes, up to `f` of which may crash, over an asynchronous network with no bound on message delay. Each process starts with an input in `{0,1}` and the protocol must satisfy **agreement** (no two correct processes decide differently), **validity** (a decided value was some process's input), and **termination** (every correct process eventually decides).

Ben-Or's crash-fault protocol requires **`n > 2f`**, equivalently `f < n/2`: a strict majority of processes are correct. That bound is what makes the central waiting step safe. Because at most `f` processes crash, waiting for `n − f` messages cannot block forever; and because `n − f > n/2`, **any two sets of `n − f` messages intersect**, so a value vouched for by every member of one collection cannot be missed entirely by another.

Majority uniqueness within a round comes from the counting gate rather than from that intersection: each process sends one phase-1 value per round, so more than `n/2` of the collected messages carrying `v` means more than `n/2` of all messages in the round carry `v`. Two different values cannot both clear that gate.

## One round is two message exchanges

Each round `r` consists of two broadcast-and-collect phases. The paper labels the messages type-1 and type-2, sent as `(1, r, …)` and `(2, r, …)`; the two phases are referred to below as report and proposal.

**Phase 1 (report).** A process broadcasts `(1, r, x)` carrying its current estimate `x`, then waits for `n − f` type-1 messages of round `r`. If **more than `n/2`** of the collected messages carry the same value `v`, the process has witnessed a majority for `v`.

**Phase 2 (proposal).** A process that saw a strict majority `v` broadcasts `(2, r, v, D)`, a decide-candidate message vouching for `v`; otherwise it broadcasts `(2, r, ?)`, a null vote. It then waits for `n − f` type-2 messages and applies a three-way rule:

- More than **`f`** D-messages for the same `v` → **decide `v`**, while continuing to send round `r + 1` messages so lagging processes can still fill their collections.
- Otherwise at least **one** D-message for some `v` → **adopt**: set `x ← v` and proceed to round `r + 1`.
- Otherwise (only nulls) → **flip a coin**: set `x` to 0 or 1, each with probability 1/2, and proceed.

The three thresholds carry the correctness argument. Requiring **more than `f`** D-messages guarantees that at least one D-message came from a correct process; a correct process emits a D only for a value backed by a strict majority in phase 1, and majorities within a round are unique, so a decision on `0` and a decision on `1` cannot coexist. The **single-D adopt rule** is the safety bridge between rounds: if any process could have decided `v`, every other correct process has seen at least one D for `v` and adopts it, so the next round begins with all estimates equal to `v` and decides. The coin fires only when the round was genuinely split, which is the state where a deterministic rule would hand the scheduler its critical message.

### Implementation sketch (Scala)

The load-bearing detail is the `n - f` cut-off on collection and the three-way rule; the transport is elided.

```scala
enum Vote:
  case D(v: Int)
  case Abstain

trait Net:
  def broadcastReport(r: Int, x: Int): Unit
  def collectReports(r: Int, count: Int): Seq[Int]      // blocks until `count` arrive
  def broadcastVote(r: Int, vote: Vote): Unit
  def collectVotes(r: Int, count: Int): Seq[Vote]

def benOr(x0: Int, n: Int, f: Int, net: Net, coin: () => Int): Int =
  var x = x0
  var r = 0
  while true do
    r += 1
    net.broadcastReport(r, x)
    // Waiting for all n would block forever once a single process has crashed.
    val reports = net.collectReports(r, n - f)
    val tally   = reports.groupMapReduce(identity)(_ => 1)(_ + _)
    val vote    = tally.find((_, c) => c > n / 2) match
      case Some((v, _)) => Vote.D(v)
      case None         => Vote.Abstain

    net.broadcastVote(r, vote)
    val ds = net.collectVotes(r, n - f).collect { case Vote.D(v) => v }
    val dTally = ds.groupMapReduce(identity)(_ => 1)(_ + _)

    dTally.find((_, c) => c > f) match
      case Some((v, _)) =>
        // Deciding is not halting: peers still need this process's round r+1
        // messages to reach their own n - f cut-offs.
        net.broadcastReport(r + 1, v)
        net.broadcastVote(r + 1, Vote.D(v))
        return v
      case None =>
        // A single D still pins the next round's estimate; only an all-null
        // round reaches the coin.
        x = ds.headOption.getOrElse(coin())
  x
```

The `coin` argument must return a **fresh, private draw each call**. A seed shared with the adversary, or a value derivable from message content, restores the predictability that the randomization exists to destroy.

## Termination, and its cost

Termination rests on a single event: a round in which **every** coin-flipping process draws the same bit. All estimates then agree entering the next round, phase 1 observes a unanimous majority, phase 2 produces D-messages from every correct process, and the `> f` threshold is met.

With independent fair local coins, the probability that as many as `n` processes land on the same value in a given round is on the order of `2^{-(n-1)}`, so the expected number of rounds is on the order of **`O(2^n)`**. Aspnes' survey makes the same point: with purely local coins the per-round agreement probability degrades exponentially in the number of processes. The protocol is correct with probability 1, but the adversary can drag expected running time to exponential in `n`.

The remedy, introduced by Rabin and refined subsequently, is a **shared coin**: a subprotocol that delivers the *same* random bit to all correct processes with constant probability regardless of message interleaving. Substituting a shared coin for the local draw leaves the round structure unchanged and yields a **constant expected number of rounds**. In the Byzantine setting, shared-coin protocols achieve `O(1)` expected rounds at the optimal resilience `f < n/3`.

## The Byzantine variant and the resilience ladder

Ben-Or's paper also gives a Byzantine-tolerant protocol with the same two-phase shape and tightened thresholds: it requires **`n > 5f`**, the phase-1 majority gate becomes `(n + f)/2`, adopting requires more than `f` D-messages, and deciding requires more than `(n + f)/2`. The `n > 5f` requirement was later improved to the optimal `n > 3f` (Bracha–Toueg), the bound at which asynchronous Byzantine fault-tolerant protocols still operate.

| Setting | Resilience | Decide threshold | Expected rounds |
|---|---|---|---|
| Crash, local coin | `n > 2f` | `> f` D-votes | `O(2^n)` |
| Crash, shared coin | `n > 2f` | `> f` D-votes | `O(1)` |
| Byzantine (Ben-Or 1983) | `n > 5f` | `> (n+f)/2` D-votes | `O(2^n)` |
| Byzantine, optimal | `n > 3f` | — | `O(1)` with shared coin |

The structural point is that FLP constrains *deterministic* protocols against *deterministic* schedulers. One bit of genuine randomness, introduced precisely where the round is deadlocked, removes the adversary's foresight. The subsequent literature — shared coins, weak coins, Byzantine variants — addresses speed rather than possibility.

## Pitfalls

- **Waiting for `n` messages instead of `n − f`.** A single crashed process never sends, so the collection never completes and the round hangs indefinitely; the `n − f` cut-off is the only reason the protocol makes progress under asynchrony.
- **Reusing or exposing the coin.** A seeded or predictable per-round bit lets the scheduler order messages against the outcome, and the run can be kept undecided again — the FLP argument applies to any protocol the adversary can simulate.
- **Deciding on `≥ f` rather than `> f` D-messages.** With exactly `f` D-messages, every one of them may have come from a process that has since crashed, so no correct process is guaranteed to be broadcasting a D; other processes can then collect an all-null round, flip coins, and enter the next round with estimates contradicting the decision.
- **Dropping the single-D adopt rule.** A process that ignores a lone D and flips a coin instead may enter the next round with an estimate contradicting a decision another process already made.
- **Deciding and halting immediately.** A process that returns without the final broadcast can leave other correct processes short of the `n − f` messages they are waiting for, so they never terminate.
- **Applying the crash thresholds under Byzantine faults.** The `n > 2f` bound and the `n/2` gate assume processes fail only by stopping; a lying process that sends different reports to different recipients defeats them, and the Byzantine variant's `(n + f)/2` gates exist for that case.
- **Assuming probability-1 termination bounds latency.** With local coins the expected round count grows as `O(2^n)`, so a run at moderate `n` can exceed any practical deadline without violating the protocol's guarantee.
