---
title: "Byzantine Fault Tolerance and PBFT: Agreeing With Liars in the Room"
date: 2026-07-30
track: distributed-systems
summary: "Why tolerating f arbitrary, possibly-malicious replicas costs 3f+1 nodes instead of Paxos's 2f+1, and how PBFT's pre-prepare / prepare / commit phases pin down a total order even when the primary lies."
reading_time: 5
tags: [byzantine-fault-tolerance, pbft, consensus, quorum, view-change, permissioned-blockchain]
sources:
  - title: "Practical Byzantine Fault Tolerance — Castro & Liskov (OSDI 1999)"
    url: "https://css.csail.mit.edu/6.824/2014/papers/castro-practicalbft.pdf"
  - title: "Practical Byzantine Fault Tolerance — the morning paper (Adrian Colyer)"
    url: "https://blog.acolyer.org/2015/05/18/practical-byzantine-fault-tolerance/"
  - title: "Practical Byzantine Fault Tolerance — Castro & Liskov, course notes (UC Berkeley CS268)"
    url: "https://people.eecs.berkeley.edu/~istoica/classes/cs268/06/notes/BFT-osdi99x2.pdf"
  - title: "Introduction to Sawtooth PBFT (Hyperledger / LF Decentralized Trust)"
    url: "https://www.lfdecentralizedtrust.org/blog/2019/02/13/introduction-to-sawtooth-pbft"
  - title: "Distributed Systems, 4th ed. — van Steen & Tanenbaum"
    url: "https://www.distributed-systems.net/index.php/books/ds4/"
---

Paxos and Raft assume the worst a node can do is stop. A crashed replica goes silent; the survivors form a majority and carry on. That model buys you `2f+1` replicas to tolerate `f` failures, and it is exactly wrong for a whole class of systems: those where a node can stay up, stay responsive, and *lie*.

A **crash fault** is fail-stop — the node either gives a correct answer or none at all. A **Byzantine fault** (the name is from the Byzantine Generals problem) is arbitrary: a compromised, buggy, or misconfigured replica can send different values to different peers, forge sequence numbers, replay old messages, or equivocate — vote yes to half the cluster and no to the other half. Majority voting no longer saves you, because the liar gets a vote too, and it does not vote consistently.

## Why 3f+1 and not 2f+1

The bound falls out of one uncomfortable overlap. To make progress you cannot wait for all `3f+1` replicas, because `f` of them might be faulty and simply never answer — so you decide after hearing from a quorum of `N - f`. But of the `N - f` that *did* answer, up to `f` might be Byzantine. Two decisions made by two different quorums must therefore overlap in at least one **honest** node, or the liars could ratify two conflicting values.

Castro and Liskov put it plainly: "it must be possible to proceed after communicating with `2f+1` replicas, since `f` replicas might be faulty and not responding. However, it is possible that the replicas that did not respond are not faulty and, therefore, `f` of those that responded might be faulty."

Work the arithmetic. Set the quorum to `2f+1`. Any two quorums of size `2f+1` drawn from `3f+1` nodes share at least `(2f+1) + (2f+1) - (3f+1) = f+1` nodes — and `f+1` nodes contain at least one honest one. That single guaranteed-honest witness is what stops equivocation. Drop to `3f` total nodes and the intersection shrinks to `f`, which could be *entirely* faulty. Hence `3f+1` is the minimum, and the paper proves it optimal.

| Failure model | Consensus example | Replicas for `f` faults | Quorum | Quorum intersection |
|---|---|---|---|---|
| Crash / fail-stop | Paxos, Raft | `2f+1` | `f+1` (majority) | `≥ 1` node |
| Byzantine / arbitrary | PBFT | `3f+1` | `2f+1` | `≥ f+1` nodes (`≥ 1` honest) |

Tolerating one Byzantine node needs four replicas; two needs seven. That is the tax you pay to survive a liar instead of a corpse.

## The three phases

PBFT runs a primary-backup scheme where one replica is the **primary** and the rest are backups. The primary for view `v` is just `p = v mod N`, so a view change rotates the role deterministically. A client sends a request to the primary, which drives it through three all-agreeing phases:

1. **Pre-prepare.** The primary assigns the request a sequence number `s`, and multicasts `⟨PRE-PREPARE, v, s, D(m)⟩` (with digest `D(m)`) to every backup. This proposes an ordering — but a faulty primary could propose different orderings to different backups, so one message is never trusted alone.
2. **Prepare.** Each backup that accepts the pre-prepare multicasts `⟨PREPARE, v, s, D(m), i⟩` to all replicas. A replica considers the request *prepared* once it holds the pre-prepare plus **`2f` matching prepares from different backups**. Prepared guarantees that non-faulty replicas agree on the order of `m` *within* view `v` — no two honest replicas can prepare the same `s` for different requests.
3. **Commit.** Once prepared, a replica multicasts `⟨COMMIT, v, s, i⟩`. When it has collected `2f+1` matching commits it is *committed-local*, executes the request, and replies to the client. The extra phase is what makes the order survive a **view change**: prepare alone holds within a view, commit makes it stick across views.

The client waits for `f+1` matching replies from different replicas before believing the result — `f+1` because at most `f` can lie, so `f+1` agreeing replies contain at least one honest voice.

Here is the prepared check a backup applies before it will move a request toward commit — the heart of "don't trust the primary":

```python
def on_prepare(self, msg):
    # msg = (view, seq, digest, sender)
    self.prepares[(msg.view, msg.seq, msg.digest)].add(msg.sender)

def is_prepared(self, view, seq, digest):
    # must have seen the primary's pre-prepare for exactly this (view, seq, digest)
    pp = self.pre_prepare.get((view, seq))
    if pp is None or pp.digest != digest:
        return False
    # ...and 2f PREPAREs from distinct *backups* that match it
    matching = self.prepares[(view, seq, digest)]
    return len(matching - {self.primary_of(view)}) >= 2 * self.f

# a replica only multicasts COMMIT once is_prepared() holds — never on
# the pre-prepare alone, because a Byzantine primary can equivocate.
```

Note the shape: `2f` prepares plus the primary's own pre-prepare is `2f+1` participants asserting this order. That is the Byzantine quorum from the table, reconstructed at every replica independently. No single message, and no single node, is ever load-bearing.

## View changes: firing a bad primary

If the primary is faulty — silent, or feeding out inconsistent pre-prepares — backups notice by timeout. A backup starts a timer when it accepts a request; if the timer expires before the request commits, it stops accepting messages for view `v` and multicasts `⟨VIEW-CHANGE, v+1, ...⟩` carrying proof of everything it had prepared. When the new primary (`(v+1) mod N`) collects `2f+1` view-change messages, it reconstructs the set of requests that *might* have committed and re-proposes them under the new view via a `NEW-VIEW` message. The prepared/committed certificates carried in those messages are what let the system change leaders without losing or reordering anything an honest client was already told had committed.

## Where this actually matters

BFT is expensive — `O(n²)` messages per request from the two all-to-all phases — so you reach for it only when nodes genuinely might be adversarial or arbitrarily broken, not merely down:

- **Permissioned blockchains.** A fixed, known validator set with no mining is exactly PBFT's setting. Hyperledger Sawtooth's PBFT engine states the rule directly — "No more than a third of the network (rounded down) can be 'out of order' or dishonest" — i.e. `3f+1` validators. Tendermint/BFT-SMaRt-style engines under Hyperledger Fabric are the same lineage.
- **Aerospace and avionics.** Flight-control and spacecraft buses use Byzantine-tolerant replication because a faulty sensor or channel can emit *plausible wrong values*, not just go dark — a classic Byzantine symptom. Van Steen & Tanenbaum's fault-tolerance chapter frames this as the general reason arbitrary-failure models exist at all.

The through-line: use crash-tolerant consensus when you trust your nodes and only fear them stopping; reach for BFT when a node staying alive and lying is inside your threat model.

**Try next:** Instantiate the snippet with `f = 1` (four replicas), feed `on_prepare` three PREPAREs where one comes from a "primary" that pre-prepared a *different* digest, and confirm `is_prepared` stays `False` — then flip that node honest and watch it cross the `2f = 2` threshold. That gap is the equivocation defense in one assertion.
