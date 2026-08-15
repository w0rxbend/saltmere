---
title: "HotStuff: BFT consensus at O(n) messages per decision"
date: 2026-07-31
track: distributed-systems
summary: "Practical Byzantine Fault Tolerance (PBFT) confirms a block with all-to-all rounds — quadratic message complexity, and a view change that is worse. HotStuff replaces the all-to-all pattern with a leader collecting threshold signatures, giving linear communication and a view change no more expensive than normal operation. It is the consensus core of Diem/LibraBFT."
reading_time: 6
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

**Gist.** Byzantine fault tolerant (BFT) state machine replication in the PBFT lineage commits a request with all-to-all voting rounds, costing **O(n²) messages per decision** and a view change whose worst-case **authenticator complexity is O(n³)**, which bounds practical deployments at small replica counts. HotStuff routes every vote to the current leader, which aggregates a quorum into a single constant-size *quorum certificate* (QC) using a threshold signature scheme, reducing each phase to **O(n) messages** and making leader replacement no more expensive than a normal round. The cost is a **third voting phase** and the operational burden of a distributed threshold-key setup.

## The cost structure PBFT imposes

The familiar BFT number — `3f + 1` replicas to tolerate `f` Byzantine ones — is linear in `f` and is not the binding constraint. The binding constraint is message count. Classic PBFT decides a request with two all-to-all rounds, `prepare` and `commit`, in which every replica broadcasts to every other. That is **O(n²) messages per decision**. When the leader is faulty and a view change is required, PBFT's view-change sub-protocol carries **O(n³) authenticators in the worst case** — the paper's Table 1 counts signatures and message authentication codes, not messages — because each replica must carry evidence about the past to every other replica. Both costs grow faster than the replica count, so each added replica is paid for more than once.

HotStuff (Yin, Malkhi, Reiter, Gueta and Abraham, PODC 2019) reduces both by routing all protocol messages through the leader instead of replica to replica.

## Star topology and quorum certificates

**Each replica sends its vote only to the current leader**, never to the group. The leader collects `2f + 1` matching votes and combines them, via a threshold signature scheme, into a single *quorum certificate* — a fixed-size object proving that a supermajority voted for a given value. The leader then broadcasts that one certificate.

Two consequences follow. First, a phase costs `n` inbound messages plus `n` outbound: **O(n) per phase**, with the leader as the only fan-out point. Second, **a QC is one signature regardless of how many replicas contributed to it**, so the certificate does not grow with `n` — the property that keeps the broadcast half linear in bytes as well as in messages.

## View change as an ordinary round

Because every step of progress has the same shape — leader proposes, replicas vote, leader forms a QC — replacing a stuck leader requires no distinct sub-protocol. Replicas that time out send a `new-view` message carrying their highest QC to the next leader; **the new leader takes the highest QC among `2f + 1` such messages and proposes on top of it**, then proceeds exactly as a normal leader would. View change therefore costs what normal operation costs. This is the sense in which the paper's *linearity* extends to leader rotation, and it is the property PBFT lacks.

## Why three phases

HotStuff runs **three chained voting phases — `prepare`, `pre-commit`, `commit` — before a value is decided**, one more than PBFT. The extra phase is what buys the uniform view change. It removes the hidden-lock problem: with two phases, a new leader can encounter a value that *might* have been committed by a predecessor while being unable to prove the matter either way, which forces the expensive reconciliation that PBFT's view change performs. **The third QC leaves every replica with enough certified history that a new leader can safely resume from the highest QC without querying anyone about the past.**

## Chaining

Rather than running three separate phases per block, *chained HotStuff* pipelines them. **Every view has a single vote round**, and one block's `prepare` QC serves simultaneously as its parent's `pre-commit` QC and its grandparent's `commit` QC. **A block is decided once it heads a three-chain: it, a child and a grandchild, each certified, with the views consecutive** — the three-chain rule. This is the structure Diem/LibraBFT, later DiemBFT v4, shipped as its production consensus.

## The safety rule

Each replica maintains `lockedQC`, the certificate it is currently locked on. On receiving a proposal `b` in view `v`, the replica votes only if `b` extends the block `lockedQC` refers to, or if `b`'s parent certificate is from a view **strictly higher** than `lockedQC`'s. The disjunction matters in that direction: an equal view is not enough, because a QC from the same view carries no evidence that the locked value was abandoned by a later quorum. On observing a three-chain `b'' <- b' <- b`, a replica decides `b''` (the grandparent) and advances the lock to the QC of `b'`.

**`lockedQC` is the safety anchor**: a replica will not vote for a proposal that abandons a value it is locked on unless the proposal provably originates from a strictly higher view. That rule together with the three-chain commit is what prevents two concurrent leaders from committing conflicting blocks.

### Implementation sketch (Scala)

```scala
final case class Qc(view: Long, block: Hash, sig: ThresholdSig)
final case class Block(hash: Hash, parent: Hash, parentQc: Qc, view: Long)

final class Replica(store: Map[Hash, Block]):
  private var lockedQc: Qc = genesisQc
  private var lastDecided: Hash = genesisQc.block

  private def extends_(descendant: Hash, ancestor: Hash): Boolean =
    Iterator.iterate(descendant)(h => store(h).parent)
      .takeWhile(store.contains)
      .contains(ancestor)

  /** Vote iff the proposal keeps the lock, or comes from a strictly higher view. */
  def onProposal(b: Block): Option[Vote] =
    if extends_(b.hash, lockedQc.block) || b.parentQc.view > lockedQc.view
    then Some(Vote(b.hash, b.view)) else None

  /** b'' <- b' <- b : lock on b', decide b'' only when the views are consecutive. */
  def onQc(qc: Qc): Unit =
    val b       = store(qc.block)
    val bPrime  = store(b.parent)     // b.parentQc certifies bPrime
    val bDPrime = store(bPrime.parent)
    if bPrime.view == b.view - 1 && bDPrime.view == bPrime.view - 1 then
      lastDecided = bDPrime.hash
    if b.parentQc.view > lockedQc.view then lockedQc = b.parentQc
```

The aggregation step is deliberately absent: the leader's combination of `2f + 1` votes into `Qc.sig` is the threshold-signature scheme's job, and it is what keeps the certificate constant-size.

## What linearity does not buy

*Responsiveness* — the leader advancing as soon as it hears from `2f + 1` replicas, at network speed, without waiting out a fixed timeout — holds only while the leader is honest and the network is delivering fast enough to assemble a quorum. **Under a faulty leader the protocol still falls back to timeouts to trigger the next view.** HotStuff makes that fallback cheap; it does not remove it.

Threshold signatures require a distributed key setup, and that setup is inherited operational work: key generation, distribution and any subsequent reconfiguration of the validator set.

## Pitfalls

- **Two-phase variants lose the cheap view change.** Dropping to two phases reintroduces the hidden-lock case, where a new leader cannot determine whether a predecessor committed a value; recovering that information is precisely the reconciliation the third phase eliminates.
- **Committing on a two-chain violates safety.** The decision rule is the three-chain; treating the parent rather than the grandparent as decided admits conflicting commits by concurrent leaders.
- **Ignoring the lock check when views advance.** Voting for any proposal from a higher view without checking that it extends `lockedQC` or carries a parent QC from a strictly higher view removes the anchor that binds concurrent leaders to a single history.
- **Assuming responsiveness under a faulty leader.** A silent or equivocating leader is detected by timeout, not by network-speed quorum formation, so tail latency is governed by the timeout schedule rather than by round-trip time.
- **Treating the leader's fan-out as free.** Linearity is a message-count property with the leader as the single fan-out point; the leader's outbound bandwidth remains proportional to `n` per view.
