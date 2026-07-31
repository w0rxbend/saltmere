---
title: "HotStuff: BFT consensus that costs O(n) per decision, not O(n²)"
date: 2026-07-31
track: distributed-systems
summary: "PBFT confirms a block by having every replica talk to every other replica — quadratic messages, and a view change that is worse. HotStuff replaces the all-to-all pattern with a leader that collects threshold signatures, giving linear communication and a view change no more expensive than normal operation. That is the idea behind Diem/LibraBFT."
reading_time: 5
tags: [hotstuff, bft, consensus, threshold-signatures, pbft, blockchain]
sources:
  - title: "Yin, Malkhi, Reiter, Gueta, Abraham — HotStuff: BFT Consensus with Linearity and Responsiveness (PODC 2019)"
    url: "https://dl.acm.org/doi/10.1145/3293611.3331591"
  - title: "HotStuff: BFT Consensus in the Lens of Blockchain (arXiv:1803.05069)"
    url: "https://arxiv.org/abs/1803.05069"
  - title: "DiemBFT v4: State Machine Replication in the Diem Blockchain (technical report)"
    url: "https://developers.diem.com/papers/diem-consensus-state-machine-replication-in-the-diem-blockchain/2021-08-17.pdf"
  - title: "Murat Demirbas — HotStuff: BFT consensus in the lens of blockchain (paper review)"
    url: "http://muratbuffalo.blogspot.com/2019/12/hotstuff-bft-consensus-in-lens-of.html"
---

Byzantine fault tolerance has a scaling problem, and it is not the one people expect. The famous number is that you need `3f + 1` replicas to tolerate `f` malicious ones, which is annoying but linear. The real cost is in the *messages*. Classic PBFT commits a request with two all-to-all rounds — prepare and commit — where every replica broadcasts to every other. That is O(n²) messages per decision, and it means PBFT works beautifully for `n = 4` and falls over at `n = 100`. Worse, when the leader is faulty and you have to change views, PBFT's view-change protocol is O(n³) in the worst case. Blockchains want hundreds of validators, so this is disqualifying.

HotStuff (Yin, Malkhi, Reiter, Gueta and Abraham, PODC 2019) fixes both by refusing to let replicas talk to each other at all.

## The two ideas

**Star topology + threshold signatures.** In HotStuff no replica broadcasts to the group. Every replica sends its vote only to the current leader. The leader collects `2f + 1` matching votes and combines them — using a threshold signature scheme — into a single fixed-size *quorum certificate* (QC) that proves "a supermajority voted for this." The leader then broadcasts that one QC. Each phase is therefore `n` messages to the leader plus `n` back out: **O(n) per phase**, and a QC is one signature regardless of how many replicas signed it.

**A view change that is just... the next round.** Because progress is always "leader proposes, replicas vote, leader forms a QC," replacing a stuck leader requires no special sub-protocol. The new leader simply starts a new view carrying the highest QC it has seen. View change costs the same as normal operation — this is what the paper means by *linearity* extending to leader rotation, and it is the property PBFT lacks.

## Why three phases, not two

HotStuff uses **three** chained voting phases — `prepare`, `pre-commit`, `commit` — before a value is decided. The extra phase compared to PBFT buys the clean view change. The subtlety it removes is the classic "hidden lock" problem: with only two phases a new leader can encounter a value that *might* have been committed by a predecessor but cannot prove it either way, forcing an expensive reconciliation. The third QC gives every replica enough certified history that a new leader can always safely pick up from the highest QC without talking to anyone about the past.

The elegant part is **chaining**. Instead of running three distinct phases for one block, "chained HotStuff" pipelines them: every view has a single vote round, and one block's `prepare` QC doubles as the previous block's `pre-commit`, and the one before that's `commit`. A block is decided once it has three descendants in the chain — a three-chain. This is exactly the structure Diem/LibraBFT (later DiemBFT v4) shipped as its production consensus.

## The commit rule, in pseudocode

The safety logic each replica runs on receiving a proposal is small enough to hold in your head:

```text
on proposal b from leader of view v:
    # safety: only vote if this extends what we're locked on,
    # or comes from a strictly newer view than our lock
    if b.parent.qc.view >= lockedQC.view or extends(b, lockedQC.block):
        send vote(b) to leader

on forming a QC for b (leader):
    if b is the 3rd link of a chain b'' <- b' <- b:
        DECIDE b''          # three-chain => commit the grandparent
    update lockedQC = qc(b')  # lock on the 2-chain
    broadcast b with its new QC
```

`lockedQC` is the safety anchor: a replica will not vote for a proposal that would abandon a value it is locked on, unless the proposal provably comes from a higher view. That single rule, plus the three-chain commit, is what makes concurrent leaders unable to commit conflicting blocks.

## What it costs you

Responsiveness — the leader advances as soon as it hears from `2f + 1` replicas, at network speed, without waiting out a fixed timeout — holds only while the leader is honest and the network is synchronous enough to gather a quorum. Under a bad leader you still fall back to timeouts to trigger the next view; HotStuff makes that fallback cheap, it does not eliminate it. And threshold signatures need a distributed key setup, which is real operational work you inherit.

**Try next:** take a 4-replica toy PBFT you have (or sketch one) and count the messages to commit one request — you will see the n² fan-out. Then replace the commit round: have replicas vote *only to the leader*, have the leader concatenate the `2f+1` votes into one "certificate" blob, and re-broadcast that. Even without real threshold crypto, watching the message count drop from n² to 2n makes HotStuff's central trick concrete.
