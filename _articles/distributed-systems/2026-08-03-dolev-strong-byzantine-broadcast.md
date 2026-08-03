---
title: "Dolev–Strong: Byzantine broadcast in f+1 rounds, and why you can't do it faster"
date: 2026-08-03
track: distributed-systems
summary: "The 1983 Dolev–Strong protocol solves synchronous Byzantine broadcast against any number of Byzantine faults — even a dishonest majority — using digital signatures and exactly f+1 rounds. The whole thing reduces to one rule: grow a chain of distinct signatures, and only trust values whose chain is long enough to guarantee an honest link."
reading_time: 5
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

Most Byzantine fault-tolerance results you meet — PBFT, HotStuff, Bracha — buy safety with a quorum bound like `n > 3f`. Dolev–Strong is the strange, wonderful exception. Given digital signatures and a synchronous network, it solves Byzantine broadcast against **any** number of faults `f`, up to `n−1` of the `n` nodes. A single honest node surrounded by liars still ends up agreeing with every other honest node. The price is rounds: exactly `f+1` of them, and van Steen & Tanenbaum's fault-tolerance chapter and the original 1983 paper both make the same point — that round count is not slack, it is optimal.

## The problem: broadcast, not agreement

Fix the terminology, because two nearby problems get muddled.

**Byzantine Broadcast (BB).** One distinguished *sender* holds an input `v`. Every node must decide an output such that:

- **Agreement** — all honest nodes output the same value, sender honest or not.
- **Validity** — if the sender is honest, that common output is exactly its input `v`.
- **Termination** — every honest node eventually halts.

**Byzantine Agreement (BA)** instead gives *every* node an input and asks them to agree (with validity meaning: if all honest nodes start equal, they stay equal). BB and BA are inter-reducible in the authenticated synchronous model — run one BB per sender to get BA — so Dolev–Strong is usually stated for broadcast, and so is this piece.

The adversary is Byzantine: faulty nodes can lie, stay silent, equivocate (tell different nodes different things), and collude. What they *cannot* do is forge a signature. That single restriction is the whole game.

## Signature chains

The protocol's only data structure is a **chain of distinct signatures over a value**.

- The sender starts it: `⟨v, sig_sender(v)⟩` is a length-1 chain.
- Anyone who accepts a length-`k` chain and wants to relay it appends *their own* signature, producing a length-`k+1` chain.

A chain is **valid for value `v`** if the sender signed first, every signature verifies, and all signers are *distinct*. The distinctness rule is what makes chain length meaningful: a length-`k` chain is a proof that `k` different identities vouched for `v`, in order, starting with the sender. A Byzantine node controlling several identities still cannot make a chain longer than the number of distinct keys it holds — and it can never omit or fake the sender's opening signature.

## The one rule: accept-set update

Each node keeps an **accept set** of values it has been convinced of. The core loop is small enough to write out. Rounds run `r = 1 … f+1`.

```python
# node i, tolerating up to f Byzantine faults among n nodes
accepted = set()          # values i is convinced of
outbox   = []             # chains to send next round

def on_round(r, incoming):        # r in 1..f+1
    for chain in incoming:
        v = chain.value
        # valid_at(chain, r): sender signed first, all sigs verify,
        # signers distinct, and length(chain) == r
        if valid_at(chain, r) and v not in accepted:
            accepted.add(v)
            if r < f + 1:                     # no point relaying after the last round
                outbox.append(chain.append_sig(i))   # length becomes r+1
    return outbox

def decide():                     # after round f+1
    return accepted.pop() if len(accepted) == 1 else BOT   # ⊥ = "sender is faulty"
```

Round 0: the sender signs `v` and sends the length-1 chain to everyone. Then for each round `r`, a node accepts a value the first time it sees a valid chain of the right length for it, appends its signature, and forwards. At the end: **exactly one value accepted → output it; zero or two-or-more → output `⊥`.** Two distinct accepted values can only happen if the sender equivocated, so `⊥` is the honest verdict of "the sender is Byzantine."

## Why validity and agreement hold

**Validity (honest sender).** An honest sender signs one value `v` and nothing else. No one can forge its signature, so no valid chain exists for any `v' ≠ v`. Every honest node accepts `v` in round 1 and never accepts a second value, so all output `v`. ✓

**Agreement** is the subtle half, and it is where `f+1` earns its keep. Suppose some honest node `i` accepts value `v`. We must show every other honest node `j` also accepts `v` by the end. Two cases:

- `i` accepts `v` in some round `r ≤ f`. Then `i` appends its signature and broadcasts the length-`(r+1)` chain in round `r+1 ≤ f+1`. That chain is valid at round `r+1`, so every honest `j` receives it and accepts `v` on time. ✓
- `i` accepts `v` in the *last* round, `r = f+1`. Then `i` saw a valid length-`(f+1)` chain — `f+1` **distinct** signers. There are at most `f` Byzantine nodes, so at least one signer in that chain is **honest**. Call it `h`. An honest `h` only signs a value when it first accepts it, in some round `< f+1`, and by the argument above an honest node that accepts early relays to everyone. So `h` already broadcast `v` to all honest nodes before the deadline, and `j` accepted it. ✓

The length-`(f+1)` requirement is a certificate that *an honest node touched this value in time*. That is the entire trick.

## Why f+1 rounds is optimal

Stop one round early — decide after round `f`. Now the last-round certificate has length `f`, `f` distinct signers, and the adversary can make **all** of them faulty. A Byzantine sender colludes with `f−1` friends to hand-craft a length-`f` chain for `v` and deliver it to exactly one honest node in the final round — too late for that node to relay. It accepts `v`; everyone else does not. Agreement breaks.

This matches the classic lower bound: any deterministic synchronous Byzantine agreement protocol tolerating `f` faults needs at least `f+1` rounds (Fischer–Lynch, and the round-reduction arguments in the Dolev–Strong lineage). Dolev–Strong meets the bound exactly. The cost is communication, not rounds: chains fan out, giving `O(n²·f)` message words in the naive version — practical work since has focused on trimming that, not the round count.

## Where it sits

Dolev–Strong is the canonical answer to "what does authentication buy you?" Lamport–Shostak–Pease's *unauthenticated* oral-message algorithm needs `n > 3f`; add unforgeable signatures and that bound vanishes — you tolerate any `f < n`. The catch is the model: it assumes **lock-step synchrony** (a known bound on message delay defining each round) and a **PKI** (everyone knows everyone's public key). Drop synchrony and FLP-style impossibilities return; drop signatures and the `3f` bound returns. Real systems that want a dishonest minority *and* asynchrony (PBFT, HotStuff) pay for it with the `n > 3f` quorum that Dolev–Strong gets to skip.

**Try next:** Implement the accept-set loop for `n=4, f=2` with a Byzantine sender that equivocates — sends a length-1 chain for `A` to node 1 and for `B` to node 2 in round 0 — then let one colluder inject a hand-built length-2 chain to a single honest node in round 2. Confirm that with the full `f+1 = 3` rounds every honest node lands on `⊥`, then rerun deciding after round 2 and watch agreement shatter.
