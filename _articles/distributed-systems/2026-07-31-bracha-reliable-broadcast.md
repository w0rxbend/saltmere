---
title: "Bracha's Byzantine Reliable Broadcast: the double-echo quorum dance"
date: 2026-07-31
track: distributed-systems
summary: "Reliable broadcast is the cheap, powerful primitive that sits below Byzantine consensus. Here's Bracha's 3-phase send/echo/ready protocol, the exact quorum thresholds, and why n>3f is the price of asynchrony."
reading_time: 5
tags: [distributed-systems, byzantine-fault-tolerance, broadcast, quorums, consensus, asynchrony]
sources:
  - title: "Bracha, Asynchronous Byzantine Agreement Protocols (Inf. Comput. 1987)"
    url: "https://www.sciencedirect.com/science/article/pii/089054018790054X"
  - title: "Decentralized Thoughts: Living with Asynchrony — Bracha's Reliable Broadcast"
    url: "https://decentralizedthoughts.github.io/2020-09-19-living-with-asynchrony-brachas-reliable-broadcast/"
  - title: "EPFL DCL: Byzantine Broadcasts and Randomized Consensus (course notes)"
    url: "https://dcl.epfl.ch/site/_media/education/sdc_byzconsensus.pdf"
  - title: "Can Bölük: Optimizing Bracha's Reliable Broadcast (2025)"
    url: "https://blog.can.ac/2025/12/25/optimizing-brachas-reliable-broadcast/"
---

You want one node to send a value to everyone and have all honest nodes agree on *what* was sent — even if the sender lies to some and stays silent to others, and even if the network delivers messages whenever it feels like it. That is **Byzantine reliable broadcast (BRB)**, and it is strictly weaker than consensus: it does not have to terminate when the sender is faulty. That weakness is exactly why it needs no leader election, no rounds, no randomization — it runs in fully asynchronous networks with a fixed protocol. Gabriel Bracha nailed it down in 1987 (*Asynchronous Byzantine Agreement Protocols*, Information and Computation 75(2):130–143), and the core is still deployed almost verbatim under modern BFT stacks.

## The three properties

Fix `n` nodes with up to `f` Byzantine. BRB with a designated sender `s` broadcasting value `v` guarantees:

- **Validity**: if `s` is honest and broadcasts `v`, every honest node eventually delivers `v`.
- **Agreement (consistency)**: no two honest nodes deliver different values.
- **Totality**: if *any* honest node delivers a value, then *every* honest node eventually delivers a value.

Note what is missing: if `s` is Byzantine, honest nodes may deliver nothing at all — but if one delivers, all do. That "all or nothing among the honest" is the whole game.

## The algorithm

Three message types, two quorum gates. Assume `n > 3f` (i.e. `n = 3f+1` at the tight bound), so a quorum of `n−f` equals `2f+1` and any two such quorums intersect in at least `f+1` nodes — of which at least one is honest.

```text
# Sender s, on brb_broadcast(v):
    send <SEND, v> to all nodes

# Every node p:
upon receiving <SEND, v> from s (first time):
    send <ECHO, v> to all nodes

upon receiving <ECHO, v> from n-f distinct nodes (= 2f+1):
    if not yet sent READY:
        send <READY, v> to all nodes

upon receiving <READY, v> from f+1 distinct nodes:   # amplification
    if not yet sent READY:
        send <READY, v> to all nodes

upon receiving <READY, v> from n-f distinct nodes (= 2f+1):   # delivery
    brb_deliver(v)
```

Two thresholds do all the work:

- **`2f+1` ECHOes → send READY.** A node only vouches for `v` after a Byzantine quorum echoed it. Because any two `2f+1`-echo sets overlap in an honest node, two *conflicting* values can never both gather `2f+1` echoes. This is where **agreement** is born.
- **`f+1` READYs → send READY (amplification).** `f+1` READYs means at least one honest node already sent READY, so it is safe to help. This bridges nodes that missed the echo quorum and prevents the protocol from stalling.
- **`2f+1` READYs → deliver.** `2f+1` READYs means at least `f+1` *honest* nodes are ready. Those `f+1` honest READYs guarantee every other honest node will hit the `f+1` amplification gate and eventually reach `2f+1` too — that is **totality**.

## Why n > 3f, concretely

The bound is not arbitrary. The delivery quorum is `2f+1`; strip out the up-to-`f` liars in it and you still have `f+1` honest nodes committed. Those `f+1` are enough to force amplification everywhere. If you tried `n = 3f` (quorum `2f`), the honest remainder could be just `f`, one short of the `f+1` needed to trigger amplification — totality breaks, and a Byzantine sender can leave the network wedged with some nodes delivered and some not. The extra node buys the strict honest majority *inside every quorum intersection*.

## What you can build with it

BRB is the substrate, not the cathedral. On top of one BRB instance per sender you get:

- **Byzantine-safe reliable multicast / consistent broadcast** for replicating commands.
- The `echo`/`ready` core of **PBFT-style** and **HotStuff-style** protocols.
- **DAG-based BFT** (Narwhal, Bullshark, and descendants) where each vertex is disseminated by exactly this primitive before ordering.

A minimal thing to try: implement the state machine above over any authenticated point-to-point transport. Model each node as a struct tracking `echoes[v]`, `readys[v]`, `sent_ready`, `delivered`, and gate on set cardinality. Then run a test harness with `n=4, f=1` where the "sender" sends `A` to two nodes and `B` to the other two — assert that no honest node ever delivers and, if you flip one node to deliver, all four do. That single adversarial test exercises both agreement and totality.

One caveat worth internalizing: the message cost is `O(n^2)` (every node echoes and readys to everyone). That quadratic is the standing target of modern work — Can Bölük's 2025 write-up adds an optimistic `⌊n/2⌋+f+1` quorum to deliver in two rounds under good conditions, and a line of papers cuts communication with threshold signatures — but they all keep Bracha's `n=3f+1`, `2f+1`, `f+1` skeleton intact.

**Try next:** Code the state machine for `n=4, f=1` and write the split-vote test above (`A` to two nodes, `B` to two) — confirm nothing delivers, then inject one delivery and watch totality drag the rest across the line.
