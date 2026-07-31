---
title: "EPaxos: leaderless consensus that only orders the commands that conflict"
date: 2026-07-31
track: distributed-systems
summary: "Multi-Paxos and Raft funnel every write through one leader, which caps throughput and forces wide-area clients to round-trip to a possibly-distant node. EPaxos removes the leader entirely: any replica commits any command, and it only pays to order two commands when they actually interfere. This article covers the dependency-graph idea, the one-round-trip fast path, and the fast-quorum subtlety that a later paper had to fix."
reading_time: 6
tags: [consensus, epaxos, paxos, leaderless, quorums, replication]
sources:
  - title: "Moraru, Andersen, Kaminsky — There Is More Consensus in Egalitarian Parliaments (SOSP 2013)"
    url: "https://www.cs.cmu.edu/~dga/papers/epaxos-sosp2013.pdf"
  - title: "EPaxos project page and reference Go implementation (efficient/epaxos)"
    url: "https://github.com/efficient/epaxos"
  - title: "Ryabinin et al. — Making Democracy Work: Fixing and Simplifying Egalitarian Paxos (OPODIS 2025)"
    url: "https://arxiv.org/abs/2511.02743"
---

Every leader-based consensus protocol shares one structural cost: a single replica orders all writes. Multi-Paxos and Raft elect a stable leader, and for each command it drives Θ(N) messages while everyone else waits. That leader is a throughput ceiling and, in a geo-distributed cluster, a latency trap — a client in Frankfurt talking to a leader in Oregon eats a transatlantic round trip on every write, even though a replica sits next door. **Egalitarian Paxos (EPaxos)**, from Moraru, Andersen and Kaminsky (SOSP 2013), asks why any replica should be special. Its answer: none should be.

## Order less, not more

The insight is that state-machine replication over-orders. Consensus traditionally agrees on a single total order of *all* commands, but most pairs of commands commute — `PUT x` and `PUT y` can be applied in either order with identical results. You only need to agree on the relative order of commands that **interfere** (touch the same key, and at least one writes).

So EPaxos doesn't build one log. Each command is proposed by whichever replica the client picked — that replica becomes the **command leader** for that one instance — and it carries two attributes:

- `deps`: the set of already-seen instances that interfere with this command.
- `seq`: a sequence number larger than the `seq` of everything in `deps`, used to break cycles when the dependency graph has them.

Committed commands form a **directed dependency graph**, not a line. At execution time each replica topologically sorts that graph (breaking cycles by `seq`), and independent commands are free to execute in parallel. Load is spread perfectly: with no distinguished leader, every replica does an equal share, and a client always talks to its *nearest* replica.

## The fast path: one round trip

Here is the common case. A replica receives command C, computes `deps` and `seq` from what it has seen locally, and sends a `PreAccept` to a fast-path quorum. If every replica in that quorum agrees with the proposed `deps`/`seq` — meaning no concurrent interfering command showed up — the command **commits in a single round trip**. No leader, no second phase.

If replicas disagree (a conflict raced in), the command leader unions the returned dependencies and runs a second `Accept` phase to fix the order — the **slow path**, still just two round trips and no election. Conflicts are the only thing that costs extra, and only conflicting commands pay.

```python
def propose(cmd):
    deps = interfering_instances_seen_locally(cmd)   # by key overlap
    seq  = 1 + max((i.seq for i in deps), default=0)

    replies = send_preaccept(cmd, deps, seq, to=fast_quorum())

    if all(r.deps == deps and r.seq == seq for r in replies):
        commit(cmd, deps, seq)          # FAST PATH: 1 round trip
    else:
        deps = union(r.deps for r in replies)         # merge conflicts
        seq  = 1 + max(r.seq for r in replies)
        send_accept(cmd, deps, seq, to=majority())    # SLOW PATH
        commit(cmd, deps, seq)
```

## The quorum subtlety worth knowing

Fast-path and slow-path quorums differ, and this is where EPaxos gets genuinely tricky. For N = 2F+1 replicas, the classic (slow-path) quorum is the usual majority, F+1 — so N=5 needs 3. The *basic* EPaxos fast path needs a larger quorum, 2F (4 out of 5), because a recovering replica must be able to reconstruct what a crashed command leader might have committed on the fast path. The paper also describes a fully-optimized variant that shrinks the fast quorum to F + ⌊(F+1)/2⌋ (3 for N=5).

That optimized quorum is exactly where later work found trouble. *Making Democracy Work* (OPODIS 2025) showed that EPaxos's recovery procedure had subtle correctness bugs and clarified the conditions the fast path must satisfy, offering a simpler, verified reformulation. The takeaway for a practitioner: EPaxos's *idea* — leaderless, conflict-only ordering with a one-round-trip common case — is sound and influential (it shows up in the lineage of protocols behind systems like Accord/Cassandra), but the fault-recovery corner is where you must trust a carefully-checked implementation rather than rolling your own from the 2013 pseudocode.

## When it wins

EPaxos pays off precisely when Raft hurts: geo-distributed clusters where client locality matters, and workloads with low interference (sharded or key-partitioned access) so most commands take the fast path. When conflicts are dense, the dependency graph thickens and you approach slow-path costs — at which point a single leader's simplicity may win back the argument.

**Try next:** clone `efficient/epaxos`, start a 5-replica cluster, and run a YCSB-style workload twice — once with keys drawn uniformly (low interference) and once with a hot 1% of keys (high interference). Watch the fraction of commands taking the slow path and the p99 latency move together; that curve is the whole trade-off made visible.
