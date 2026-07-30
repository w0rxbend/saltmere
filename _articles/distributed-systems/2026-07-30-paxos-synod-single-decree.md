---
title: "Single-Decree Paxos: How a Majority Agrees on One Value"
date: 2026-07-30
track: distributed-systems
summary: "The Synod protocol at the heart of Paxos: two phases, proposal numbers, and the majority-quorum overlap that makes a single chosen value stick — even when proposers race and messages drop."
reading_time: 6
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

Consensus is the problem of getting a set of processes to agree on a single value, in a way that survives crashes and dropped messages. **Single-decree Paxos** — what Lamport originally called the *Synod* protocol in "The Part-Time Parliament" — solves exactly one instance of that: agree on one value, once, forever. Multi-Paxos and Raft are what you build on top when you want a whole *log* of agreed values; the Synod is the atom they're made of.

The hard part isn't the happy path. It's that multiple proposers can propose concurrently, messages arrive out of order, and nodes crash and recover — yet the protocol must never let two processes believe *different* values were chosen. Paxos gets there with three roles, two phases, and one arithmetic fact about majorities.

## The three roles

- **Proposers** suggest values.
- **Acceptors** vote. They are the fault-tolerant memory of the system; a value is *chosen* when a majority of acceptors have accepted it.
- **Learners** find out which value was chosen and act on it.

One physical node usually plays all three. What matters is the acceptor logic, because acceptors are where safety lives.

## Proposal numbers

Every proposal carries a globally unique, monotonically increasing **proposal number** `n`. These are not values — they're a priority/ordering device. Uniqueness is easy: give each proposer a disjoint slice of the number space (e.g. `round * num_proposers + proposer_id`). Higher `n` always wins a tie of wills; that's what lets a later proposer safely override a stalled earlier one without corrupting an already-chosen value.

## The two phases

**Phase 1 — Prepare / Promise.** A proposer picks a fresh `n` and sends `prepare(n)` to a majority of acceptors. An acceptor that has not already promised a higher number replies with a **promise**: "I will never again accept anything numbered below `n`," and — crucially — it reports back the highest-numbered proposal `(n_accepted, v_accepted)` it has already accepted, if any.

**Phase 2 — Accept / Accepted.** If the proposer collects promises from a majority, it must now choose the value to propose. This is the subtle rule that makes Paxos safe:

- If *any* promise carried an already-accepted value, the proposer **must** propose the value with the highest `n_accepted` among them. It does not get to use its own value.
- If no promise carried a value, the proposer is free to propose its own.

It then sends `accept(n, v)` to a majority. An acceptor accepts unless it has since promised a number greater than `n`. Once a majority has accepted `(n, v)`, `v` is chosen.

## Why a majority quorum is safe

Both phases require a **majority**. Any two majorities of the same acceptor set share at least one member (pigeonhole). That single overlapping acceptor is the linchpin: it cannot forget what it accepted, so any later proposer's Phase 1 is guaranteed to *see* an already-chosen value and is forced by the Phase 2 rule to re-propose it.

Lamport states the resulting invariant as **P2c**:

> "For any v and n, if a proposal with value v and number n is issued, then there is a set S consisting of a majority of acceptors such that either (a) no acceptor in S has accepted any proposal numbered less than n, or (b) v is the value of the highest-numbered proposal among all proposals numbered less than n accepted by the acceptors in S."

The consequence, in plainer terms: *once a value is chosen, every higher-numbered proposal that gets issued carries that same value.* Agreement is preserved not by locking, but by forcing new proposers to defer to what the quorum already remembers.

## A walk-through

Five acceptors `{a, b, c, d, e}`; a majority is 3.

1. Proposer **X** runs `prepare(1)`, gets promises from `{a, b, c}`. None has accepted anything, so X is free. X sends `accept(1, "apple")` to `{a, b, c}` — but only `a` and `b` receive it before X crashes. `"apple"` is accepted by 2 acceptors: **not** a majority, so **not yet chosen**.
2. Proposer **Y** runs `prepare(2)`, reaching `{c, d, e}`. Acceptor `c` has accepted nothing; `d`, `e` likewise. So Y sees no prior value and is free to pick — say `accept(2, "banana")` to `{c, d, e}`. All three accept. `"banana"` is now chosen by a majority.
3. Proposer **X** recovers, retries `accept(1, "apple")` to `c`. But `c` promised `2 > 1` in step 2, so it **rejects**. `"apple"` can never reach a majority. Safety holds: exactly one value, `"banana"`, was ever chosen.

Now the *other* ordering, showing the override rule bite:

1. X gets `"apple"` accepted by a majority `{a, b, c}` under `n=1`. Chosen.
2. Y runs `prepare(2)` to `{c, d, e}`. Acceptor `c` replies with `(1, "apple")`. Because at least one promise carried a value, Y is **forced** to propose `"apple"`, not its own value. Y sends `accept(2, "apple")`. The chosen value is preserved.

## An acceptor, in code

The acceptor is tiny — two handlers over three persistent variables. Persistence matters: these must survive a crash, or the majority-overlap argument breaks.

```python
class Acceptor:
    def __init__(self):
        self.promised_n = None       # highest n we've promised (Phase 1)
        self.accepted_n = None       # n of the proposal we accepted (Phase 2)
        self.accepted_v = None       # its value

    def on_prepare(self, n):
        # Promise only if n beats every promise we've made.
        if self.promised_n is None or n > self.promised_n:
            self.promised_n = n
            self._persist()          # MUST hit stable storage before replying
            return ("promise", n, self.accepted_n, self.accepted_v)
        return ("nack", self.promised_n)

    def on_accept(self, n, v):
        # Accept unless we've since promised something strictly higher.
        if self.promised_n is None or n >= self.promised_n:
            self.promised_n = n
            self.accepted_n = n
            self.accepted_v = v
            self._persist()
            return ("accepted", n, v)
        return ("nack", self.promised_n)
```

The proposer's job is the mirror image: gather a majority of `promise`s, apply the "adopt the highest accepted value" rule, then send `accept`. If it's out-voted by a `nack`, it bumps `n` and starts over.

## How it differs from Raft

Raft (covered in its own article here) solves the same underlying problem but makes deliberately different bets. A quick contrast:

| | Single-decree Paxos | Raft |
|---|---|---|
| Scope | one value | a replicated log |
| Leadership | none required; any proposer may propose | a single elected leader drives all writes |
| Concurrency | competing proposers are normal (and can livelock) | one leader at a time by design |
| Design goal | minimal, provable safety core | understandability (its paper's explicit thesis) |
| Log holes | N/A (single value) | disallowed — the log must stay contiguous |

Raft essentially bakes in a strong leader and a contiguous-log constraint so that the common case has no dueling proposers and no per-slot value-selection reasoning. Paxos leaves those choices to you, which is why raw single-decree Paxos is elegant but rarely deployed unmodified — you almost always want Multi-Paxos with a distinguished proposer to avoid the livelock two proposers can create by repeatedly out-numbering each other in Phase 1. As Howard and Mortier argue, the two algorithms are closer than folklore suggests; the differences are mostly in these engineering commitments, not in the consensus core.

**Try next:** Implement the `Acceptor` above plus a proposer, then write a test that runs the walk-through's second ordering — X chooses `"apple"`, then Y with a higher `n` must be *forced* to re-propose `"apple"` — and assert Y can never get `"banana"` chosen. Then delete the `_persist()` calls, restart an acceptor mid-round, and watch the majority-overlap guarantee collapse.
