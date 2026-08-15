---
title: "Dolev–Strong: Byzantine broadcast in f+1 rounds, and why no protocol is faster"
date: 2026-08-03
track: distributed-systems
summary: "The 1983 Dolev–Strong protocol solves synchronous Byzantine broadcast against any number of Byzantine faults — even a dishonest majority — using digital signatures and exactly f+1 rounds. The protocol reduces to one rule: grow a chain of distinct signatures, and accept only values whose chain is long enough to guarantee an honest link."
reading_time: 6
tags: [byzantine, consensus, dolev-strong, broadcast, signatures, synchrony]
sources:
  - title: "Dolev & Strong — Authenticated Algorithms for Byzantine Agreement (SIAM J. Comput. 12(4):656–666, 1983)"
    url: "https://epubs.siam.org/doi/10.1137/0212045"
  - title: "Decentralized Thoughts — Dolev-Strong Authenticated Broadcast (Abraham & Shi)"
    url: "https://decentralizedthoughts.github.io/2019-12-22-dolev-strong/"
  - title: "Tim Roughgarden — Foundations of Blockchains, Lecture #2: The Dolev-Strong Protocol"
    url: "https://timroughgarden.github.io/fob21/l/l2.pdf"
  - title: "Elaine Shi — Foundations of Distributed Consensus and Blockchains (book draft)"
    url: "https://elaineshi.com/docs/blockchain-book.pdf"
  - title: "James R. Lee — CSE 422 notes, Ch. 3: Byzantine Broadcast and the Dolev-Strong Protocol"
    url: "https://homes.cs.washington.edu/~jrl/cse422wi24/notes/blockchain_3.pdf"
---

**Gist.** Byzantine broadcast requires every honest node to output the same value even when the sender itself lies, and quorum-based protocols buy that property with the bound `n > 3f`. Dolev–Strong replaces the quorum with **unforgeable digital signatures under lock-step synchrony**, tolerating any `f ≤ n−1` faults by accepting a value only when it carries a chain of `r` distinct signatures in round `r`. The cost is latency and bandwidth: **exactly `f+1` rounds**, matching the classical lower bound for deterministic synchronous protocols, and chains that fan out across the network.

## The problem: broadcast, not agreement

Two nearby problems are routinely conflated, so the terminology is fixed first.

**Byzantine Broadcast (BB).** One distinguished *sender* holds an input `v`. Every node must decide an output such that:

- **Agreement** — all honest nodes output the same value, sender honest or not.
- **Validity** — if the sender is honest, that common output is exactly its input `v`.
- **Termination** — every honest node eventually halts.

**Byzantine Agreement (BA)** instead gives *every* node an input and asks the nodes to agree, with validity meaning that if all honest nodes start equal they stay equal. BB and BA are inter-reducible in the authenticated synchronous model — running one BB instance per sender yields BA — so Dolev–Strong is customarily stated for broadcast, as it is here.

The adversary is Byzantine: faulty nodes may lie, stay silent, equivocate (send different values to different nodes), and collude. The one capability withheld is **forging a signature of a node they do not control**. That single restriction carries the entire protocol.

## Signature chains

The protocol's only data structure is a **chain of distinct signatures over a value**.

- The sender opens the chain: `⟨v, sig_sender(v)⟩` has length 1.
- A node that accepts a length-`k` chain and relays it appends *its own* signature, yielding length `k+1`.

A chain is **valid for value `v`** when the sender signed first, every signature verifies over `v`, and all signers are **distinct**. Distinctness is what makes length meaningful: a length-`k` chain is evidence that `k` different identities vouched for `v`, in order, beginning with the sender. A Byzantine coalition cannot lengthen a chain on its own beyond the number of distinct private keys it holds, and cannot produce any chain at all without the sender's opening signature — so a sender that never signs `v'` makes `v'` unacceptable to every honest node, forever.

## The one rule: accept-set update

Each node maintains an **accept set** of values it has been convinced of. Rounds run `r = 1 … f+1`, and the round number is part of the validity test: **in round `r`, only chains of length exactly `r` are admissible**. That coupling is what forces a value to make progress every round or die.

The state machine per node is three transitions:

1. **Receive** the round's messages.
2. For each chain valid at round `r` whose value is not already in the accept set: **add the value**, and if `r < f+1`, append the node's own signature and send the length-`(r+1)` chain to all nodes in round `r+1`. A value already accepted is never relayed again, which bounds each node to one relay per value.
3. After round `f+1`, **decide**: exactly one value accepted → output it; zero or two-or-more accepted → output `⊥`.

Two distinct accepted values are possible only if the sender signed two values, so `⊥` is the honest verdict "the sender is Byzantine", and it is a common output, which satisfies agreement.

### Implementation sketch (Scala)

```scala
// `sign` and `verify` are the underlying signature primitives, omitted here.
final case class Chain(value: String, signers: List[NodeId], sigs: List[Array[Byte]])

final class Node(id: NodeId, sender: NodeId, f: Int, pki: Map[NodeId, PublicKey]):
  private var accepted: Set[String] = Set.empty

  // Length is checked against the round: a chain that arrives late is worthless.
  private def validAt(c: Chain, r: Int): Boolean =
    c.signers.length == r &&
      c.signers.headOption.contains(sender) &&
      c.signers.distinct.length == c.signers.length &&
      c.signers.zip(c.sigs).forall((s, sig) => verify(pki(s), c.value, sig))

  // Folded rather than filtered: `accepted` must grow as the batch is scanned, so a
  // second chain for the same value in the same round is not relayed twice.
  def onRound(r: Int, incoming: Seq[Chain]): Seq[Chain] =
    incoming.foldLeft(Vector.empty[Chain]) { (out, c) =>
      if !validAt(c, r) || accepted.contains(c.value) then out
      else
        accepted += c.value
        if r == f + 1 then out // relaying after the final round changes no decision
        else out :+ c.copy(signers = c.signers :+ id, sigs = c.sigs :+ sign(id, c.value))
    }

  def decide(): Option[String] =
    if accepted.sizeIs == 1 then accepted.headOption else None // None encodes ⊥
```

## Why validity and agreement hold

**Validity (honest sender).** An honest sender signs one value `v` and nothing else. Signatures are unforgeable, so no valid chain exists for any `v' ≠ v`. Every honest node accepts `v` in round 1 and never accepts a second value, so every honest node outputs `v`.

**Agreement** is the subtle half, and it is where `f+1` earns its keep. Suppose an honest node `i` accepts `v`; every other honest node `j` must accept `v` before the deadline. Two cases exhaust the possibilities:

- `i` accepts `v` in a round `r ≤ f`. Then `i` appends its signature and sends the length-`(r+1)` chain in round `r+1 ≤ f+1`. Synchrony delivers it, the chain is valid at round `r+1`, so `j` accepts `v` in time.
- `i` accepts `v` in the final round `r = f+1`. Then `i` observed a valid chain with **`f+1` distinct signers**. At most `f` nodes are Byzantine, so at least one signer `h` is **honest**. An honest node signs a value only in the round it first accepts it, necessarily some round `< f+1`, and by the previous case an honest node that accepts before the last round relays to everyone. So `h` already delivered `v` to `j` in time.

The length-`(f+1)` requirement is therefore a certificate that **an honest node touched this value early enough to have relayed it**. That is the whole mechanism.

## Why f+1 rounds is optimal

Deciding one round early makes the final certificate length `f`, that is `f` distinct signers, and an adversary controlling `f` nodes can supply all of them. A Byzantine sender and `f−1` colluders hand-craft a length-`f` chain for `v` and deliver it to exactly one honest node in the final round — too late for that node to relay. It accepts `v`; no other honest node does. Agreement breaks.

This matches the classical lower bound: a deterministic synchronous protocol tolerating `f` faults requires at least `f+1` rounds, and Dolev–Strong meets it exactly. The residual cost is communication rather than latency: every accepted value is relayed to all `n` nodes, and each chain carries up to `f+1` signatures, so bandwidth grows with both the network size and the fault bound. Subsequent work has targeted that volume, not the round count.

## Where it sits

Dolev–Strong is the canonical answer to the question of what authentication buys. The unauthenticated oral-message algorithm of Lamport, Shostak and Pease requires `n > 3f`; adding unforgeable signatures removes that bound entirely and admits any `f < n`. The price is model assumptions: **lock-step synchrony**, meaning a known upper bound on message delay that defines each round, and a **public-key infrastructure (PKI)** in which every node knows every other node's public key. Without synchrony, asynchronous impossibility results return; without signatures, the `3f` bound returns. Protocols that want both a dishonest minority and partial synchrony, such as PBFT and HotStuff, pay for it with the `n > 3f` quorum Dolev–Strong avoids.

## Pitfalls

- **Omitting the length-equals-round check.** A node that accepts any valid chain regardless of length can be fed a stale length-1 chain in the final round by a colluding sender; it accepts a value it can no longer relay, and honest nodes diverge.
- **Accepting duplicate signers.** If distinctness is not enforced, a single Byzantine node with one key can pad a chain to length `f+1` by signing repeatedly, and the certificate stops implying that an honest node ever saw the value.
- **Deciding on a non-empty accept set instead of a singleton.** With an equivocating sender, honest nodes hold two values; picking one deterministically (for example, the smallest) still diverges unless every honest node holds the *same* two, which the protocol does not guarantee until the final round completes.
- **Halting after the first accepted value.** A node that stops relaying because it "already knows the answer" withholds the chain that carries the honest link, breaking the last-round argument for agreement.
- **Running under partial synchrony.** The agreement proof uses the assumption that a message sent in round `r` arrives before round `r+1` begins; if the round timeout is shorter than the real delay bound, a late chain arrives out of round, is rejected on length, and honest nodes disagree.
- **Treating `f` as the observed fault count.** The round count and the certificate length are both fixed by the *tolerated* `f` chosen in advance; lowering `f` at runtime to save rounds shortens the certificate and reintroduces the one-round-early attack.
