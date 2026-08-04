---
title: "Conits and continuous consistency: three dials between strong and eventual"
date: 2026-08-04
track: distributed-systems
summary: "Strong and eventual consistency are two endpoints of a continuum. Yu and Vahdat's conit model measures the space between them along three independent axes — numerical, ordering, and staleness deviation — and lets an application bound each one. Here is what the three dimensions mean, the textbook two-replica example, an implementable sketch of numerical-bound enforcement, and how Azure Cosmos DB exposes the same idea as a knob."
reading_time: 6
tags: [consistency, replication, conits, bounded-staleness, distributed-systems]
sources:
  - title: "Van Steen & Tanenbaum — Distributed Systems (4th ed.), Chapter 7: Consistency and Replication"
    url: "https://www.distributed-systems.net/index.php/books/ds4/"
  - title: "Yu & Vahdat — Design and Evaluation of a Conit-Based Continuous Consistency Model for Replicated Services (ACM TOCS 20(3), 2002)"
    url: "https://dl.acm.org/doi/10.1145/566340.566342"
  - title: "Yu & Vahdat — Design and Evaluation of a Continuous Consistency Model for Replicated Services (OSDI 2000, free PDF)"
    url: "https://www.usenix.org/legacy/events/osdi2000/full_papers/yuvahdat/yuvahdat.pdf"
  - title: "TACT — Tunable Availability and Consistency Tradeoffs (Duke ISSG project page)"
    url: "https://www2.cs.duke.edu/ari/issg/TACT/"
  - title: "Azure Cosmos DB — Consistency levels (bounded staleness)"
    url: "https://learn.microsoft.com/en-us/azure/cosmos-db/consistency-levels"
---

Most consistency taxonomies are a ladder: linearizable at the top, eventual at the bottom, a few named rungs in between. The problem with a ladder is that you have to pick a rung. Yu and Vahdat's **continuous consistency** model, built into the TACT toolkit, replaces the ladder with a *continuum*: it measures how far a replicated store is currently drifting from a perfectly consistent state, and lets the application cap that drift by however much it can tolerate. Strong consistency is just the point where all the caps are set to zero; eventual consistency is where they are all set to infinity. Everything useful lives in between.

This is a **data-centric** model, and that is the first thing to get straight. The [previous article](../2026-07-30-client-centric-consistency-session-guarantees/) covered *client-centric* session guarantees, which constrain what one user's own session is allowed to observe and say nothing about how replicas relate to each other. Continuous consistency is the opposite viewpoint: it bounds how far the *replicas themselves* may diverge from the ideal final state, for all observers at once. No per-session bookkeeping — just numeric limits on global drift.

## Three independent dimensions of inconsistency

The model's central claim is that "how inconsistent is this replica right now" is not one number but three, and they vary independently. Van Steen and Tanenbaum present them as the three axes a conit's bounds are enforced along:

| Dimension | Question it answers | Bounded by |
|---|---|---|
| **Numerical deviation** | How far is my value from the fully-converged value? | Number *and* total weight of writes I haven't seen yet |
| **Ordering deviation** | How many of my applied writes are still tentative and could be reordered? | Count of outstanding (uncommitted) writes at the replica |
| **Staleness deviation** | How old is the oldest write I'm still missing? | Wall-clock time since that write was accepted elsewhere |

**Numerical deviation** captures value drift. If a conit holds a stock count and other replicas have accepted sales this replica hasn't heard about, the count is numerically stale by the summed *weight* of those missing writes (weight is application-defined — often the magnitude of the change). It has both an absolute form (total unseen weight) and a relative form (a percentage of the true value).

**Ordering deviation** — the "staleness of writes not yet applied in final order" axis — is about *tentative* writes. In an optimistic replica, a locally-accepted write is provisional: it may be reordered or rolled back once global commit order is known. Ordering deviation is simply the number of such outstanding writes. Bound it at zero and every write must commit before it is visible; loosen it and the replica may accept many speculative writes it might later have to reshuffle.

**Staleness deviation** is the time axis: the gap between now and the acceptance time of the oldest write elsewhere that this replica has not yet seen. A 10-second staleness bound says "no write may remain invisible to me for longer than 10 seconds," independent of how many writes or how much weight are involved.

## Conits: the unit the bounds apply to

A **conit** — consistency unit — is the granularity over which these three bounds are enforced. It is an application-defined logical or physical unit: a single record, a group of related fields, a whole table. Granularity is a real trade. A coarse conit (one conit for a huge dataset) is cheap to track but causes *false sharing* — an unrelated write anywhere in the conit counts against everyone's bounds. A fine conit (one per field) is precise but multiplies the vector-clock and anti-entropy overhead. You size conits the way you size locks.

## The textbook two-replica example

The canonical figure has a conit containing two variables, `x` and `y`, replicated at A and B. Focus on replica A. It has applied four operations to the conit; three of them are its *own*, still-tentative writes (say `y += 2`, `y += 5`, `x += 4`), and one is a write it received from B and has already made permanent (`x += 2`, tagged `<5,B>`). A summarizes what it has seen with a vector clock like `(15, 5)`: 15 of its own operations, 5 of B's.

From this, A reads off its deviations directly:

- **Ordering deviation = 3.** A is holding three tentative writes of its own that are not yet globally committed and could still be reordered.
- **Numerical deviation = (1, 5).** A knows (from clocks exchanged during anti-entropy) that B has accepted **1** write A has not yet seen, and that write's **weight is 5**. So A's value of the conit may be off by up to 5 units.

B keeps the symmetric bookkeeping about A. The moment either replica's tracked deviation would exceed its configured bound, it stops being lazy and pushes or pulls writes so the bound is restored. That push is the whole mechanism.

## Enforcing a numerical bound, concretely

Numerical deviation is the easiest dimension to implement, because it is additive. The trick TACT uses: split the *global* numerical-error budget across the N replicas, so each replica is individually responsible for not letting the weight it has accepted-but-not-yet-pushed grow past its share. If every replica honors its slice, the total is bounded by construction.

```python
class ConitReplica:
    def __init__(self, replica_id, num_replicas, global_bound):
        self.id = replica_id
        # each replica may induce at most this much error on any peer
        self.local_bound = global_bound / num_replicas
        # weight of writes accepted here but not yet pushed to each peer
        self.unpropagated = {p: 0.0 for p in range(num_replicas) if p != replica_id}
        self.log = []

    def accept_write(self, w):
        # w.weight = application-defined magnitude of the change
        self.apply(w)
        self.log.append(w)
        for peer in self.unpropagated:
            self.unpropagated[peer] += abs(w.weight)
        self.enforce_numerical_bound()

    def enforce_numerical_bound(self):
        for peer, drift in self.unpropagated.items():
            # would this peer's view of us exceed its share of the budget?
            if drift >= self.local_bound:
                self.push_to(peer)   # compulsory anti-entropy

    def push_to(self, peer):
        send(peer, self.log_since(peer))   # ship the missing writes
        self.unpropagated[peer] = 0.0      # peer is now caught up on our writes
```

The shape generalizes. For **staleness**, replace the weight accumulator with a timer per peer and push when `now - oldest_unpushed_accept_time` nears the bound. For **ordering**, the replica triggers the write-commitment protocol (agree on a global order, make tentative writes permanent) once its count of outstanding writes reaches the limit.

## The trade you are actually making

Each bound is a dial from consistency toward availability and performance. Tight bounds mean frequent, eager propagation: more messages, more synchronous waiting, less tolerance for a slow or partitioned peer — but views stay close to converged. Loose bounds mean the replica coasts on local state, absorbing writes and serving reads without talking to anyone, right up until a bound trips. Crucially, the three dials are *independent*: an inventory service might demand a tight numerical bound (never oversell by more than 5 units) while happily tolerating seconds of staleness and dozens of tentative writes. You spend your consistency budget exactly where the application is sensitive.

## The same idea, shipped

Azure Cosmos DB's **bounded staleness** level is continuous consistency's staleness and ordering axes exposed as a product knob. You configure two bounds — **K**, a number of versions (writes), and **T**, a time interval — and the store guarantees cross-region reads lag the latest write by at most K versions *or* T seconds, whichever trips first. That is precisely the ordering-count and time-staleness dimensions, made into a dropdown. The floors reflect the cost of enforcement: single-region accounts allow a minimum of K = 10 writes or T = 5 seconds, while multi-region accounts require at least K = 100,000 writes or T = 300 seconds. Sitting between Strong and Session in Cosmos DB's five levels, bounded staleness is continuous consistency with the numerical axis left to the application.

**Try next:** Extend the `ConitReplica` sketch into a two-node simulation: drive random weighted writes into both replicas, run lazy anti-entropy on a timer, and assert after every step that neither replica's `unpropagated` weight ever exceeds `local_bound`. Then add a staleness timer as a second, independent bound and watch which one trips first under a bursty vs. a steady write load.
