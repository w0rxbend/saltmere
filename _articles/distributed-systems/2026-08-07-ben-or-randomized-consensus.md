---
title: "Ben-Or's randomized consensus: how a coin flip walks around FLP"
date: 2026-08-07
track: distributed-systems
summary: "FLP says no deterministic asynchronous protocol can guarantee consensus with even one crash. Ben-Or's 1983 answer is disarmingly simple: when the round is deadlocked, every node flips a private coin. This is the full two-phase round, the exact thresholds, why it needs n > 2f, and why local coins cost you O(2^n) expected rounds."
reading_time: 6
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

FLP tells you the bad news: in a fully asynchronous network where one process might crash, no *deterministic* protocol can guarantee agreement *and* termination. The proof hangs on the adversary always finding one "critical" message it can delay to keep the system undecided. That trick works because the scheduler can predict what each node will do next. Michael Ben-Or's 1983 paper (*Another Advantage of Free Choice*, PODC) removes exactly that predictability: when a round ends without a clear majority, every node flips a private, fair coin. The adversary can no longer steer a run it cannot foresee, and the protocol terminates with probability 1. It is one of the shortest routes from an impossibility theorem to a working algorithm in all of distributed systems.

## The setup

We want binary consensus among `n` processes, up to `f` of which may crash, over an asynchronous network (no message-delay bound). Each process starts with an input in `{0,1}` and must satisfy the usual three properties: **agreement** (no two correct processes decide differently), **validity** (a decided value was some process's input), and **termination** (every correct process eventually decides). Ben-Or's crash-fault protocol needs `n > 2f` — a strict majority of processes must be correct, equivalently `f < n/2`. That majority is what makes the "collect `n − f` messages" step safe: any two such collections overlap, so two correct nodes can never build contradictory majorities in the same round.

## One round = two message exchanges

Every round `r` has two broadcast-and-collect phases. The paper labels its messages type-1 and type-2; lecture notes call them the **report/propose** phase and the **proposal/ratify** phase. Same thing.

**Phase 1 (report).** Broadcast `(1, r, x)` carrying your current estimate `x`. Wait until `n − f` type-1 messages for round `r` arrive. Now check them: if **more than `n/2`** of them carry the same value `v`, you have witnessed a majority for `v`; otherwise you have not.

**Phase 2 (proposal).** If phase 1 gave you a strict majority `v`, broadcast `(2, r, v, D)` — a *D* ("decide-candidate") message vouching for `v`. If it did not, broadcast `(2, r, ?)`, a null vote. Wait until `n − f` type-2 messages arrive, then apply the decision rule:

- If you received **more than `f`** D-messages all for the same `v` → **decide `v`** (and do one more round so laggards can catch up).
- Else if you received **at least one** D-message for some `v` → **adopt** it: set `x ← v` and continue to round `r + 1`.
- Else (only nulls) → **flip a coin**: set `x ← 0 or 1`, each with probability 1/2, and continue.

The three-way rule is the whole design. The `> f` D-messages needed to decide guarantee at least one D came from a *correct* process; since a correct process only sends a D for a value it saw a strict majority behind in phase 1, and majorities in the same round are unique, no correct node can decide `0` while another decides `1`. The single-D adopt rule is the safety bridge: if *anyone* could have decided `v`, everyone else at least adopts `v`, so the next round starts already leaning the right way. And the coin is the liveness escape — it only fires when the round was genuinely split, and it is precisely what the adversary cannot plan around.

## One node's round loop

```python
import random

def ben_or(node, x, n, f, net):
    # x: this node's initial estimate in {0, 1}
    r = 0
    while True:
        r += 1

        # --- Phase 1: report your estimate ---
        net.broadcast(("R", r, x))
        reports = net.collect(kind="R", round=r, count=n - f)  # wait for n-f

        counts = tally(v for (_, _, v) in reports)
        maj = argmax_value(counts)
        proposal = maj if counts[maj] > n / 2 else None      # strict majority?

        # --- Phase 2: proposal / ratification ---
        net.broadcast(("P", r, proposal))                    # None == "?"
        props = net.collect(kind="P", round=r, count=n - f)  # wait for n-f

        ds = [v for (_, _, v) in props if v is not None]     # the D-votes
        dcount = tally(ds)

        if ds:
            v = mode(ds)
            if dcount[v] > f:            # >f D-votes for v  ->  DECIDE
                net.broadcast(("R", r + 1, v))  # help stragglers, then:
                return v
            x = v                        # saw a D  ->  ADOPT it
        else:
            x = random.randint(0, 1)     # no D at all  ->  FLIP A COIN
```

Two subtleties worth internalizing. First, `net.collect` waits for exactly `n − f` messages and no more — waiting for all `n` would let a single crash hang the round forever, which is the asynchrony trap FLP exploits. Second, the coin flip must be a *fresh, private* draw each time; reusing a seed or letting the value leak hands the scheduler back its predictive power.

## Why it terminates — and why it can be slow

Termination rides on one event: a round in which **every** coin-flipping node happens to draw the same bit. When that occurs, all estimates for the next round converge, phase 1 sees a unanimous majority, phase 2 produces a flood of D-messages, and everyone decides. With independent, fair local coins, the probability that up to `n` nodes all land on the same value in a given round is roughly `2^{-(n-1)}` — exponentially small in `n`. So the *expected* number of rounds is on the order of `O(2^n)`. That is the price of "free choice" done locally: it is correct with probability 1, but a real adversary can drag the expected running time to exponential (Aspnes' survey states the per-round termination probability "may be exponentially small as a function of the number of processes").

The fix, due to Rabin and refined by many since, is a **shared coin**: a subprotocol that gives all correct nodes the *same* random bit with constant probability, however the messages interleave. Swap the local `random.randint` for a shared-coin call and the same skeleton terminates in a *constant* expected number of rounds. For the Byzantine setting, shared-coin protocols achieve `O(1)` expected rounds at the optimal resilience `f < n/3`.

## The Byzantine variant, and the resilience ladder

Ben-Or's paper also gives a Byzantine-tolerant protocol with the identical two-phase shape, but the thresholds tighten to absorb lying nodes: it requires `n > 5f`, the phase-1 majority gate becomes `(n + f)/2`, adopting needs `f + 1` D-messages, and deciding needs more than `(n + f)/2`. That `n > 5f` was later improved to the optimal `n > 3f` (Bracha–Toueg), which is the bound modern asynchronous BFT still lives at.

| Setting | Resilience | Decide threshold | Expected rounds |
|---|---|---|---|
| Crash, local coin | `n > 2f` | `> f` D-votes | `O(2^n)` |
| Crash, shared coin | `n > 2f` | `> f` D-votes | `O(1)` |
| Byzantine (Ben-Or 1983) | `n > 5f` | `> (n+f)/2` D-votes | `O(2^n)` |
| Byzantine, optimal | `n > 3f` | — | `O(1)` with shared coin |

The lasting lesson is structural: FLP is not a wall, it is a statement about *deterministic* schedulers beating *deterministic* protocols. Add one bit of genuine randomness at the exact point where the round is deadlocked, and the adversary loses its foresight. Everything after Ben-Or — shared coins, weak coins, Byzantine variants — is engineering to make that idea *fast*, not to make it *work*.

**Try next:** implement the loop above for `n = 4, f = 1` with independent local coins and a message scheduler you control. Run 10,000 trials from a split input (two nodes start `0`, two start `1`), record rounds-to-decision, and plot the distribution — you should see a geometric-ish tail consistent with a per-round success probability near `2^{-3}`. Then replace the local flip with a trivial "shared coin" (all nodes read the same seeded bit for round `r`) and watch the tail collapse to one or two rounds.
