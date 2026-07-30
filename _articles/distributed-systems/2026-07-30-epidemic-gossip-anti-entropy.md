---
title: "Epidemic protocols: anti-entropy, rumor spreading, and why gossip converges"
date: 2026-07-30
track: distributed-systems
summary: "Gossip isn't a vibe — it's a family of protocols with a convergence proof. Anti-entropy trades bandwidth for a guarantee that replicas eventually match; rumor mongering trades that guarantee for speed. Here's the distinction, the push/pull variants, and a runnable anti-entropy round in ~30 lines."
reading_time: 6
tags: [gossip, epidemic-protocols, anti-entropy, replication, eventual-consistency, membership]
sources:
  - title: "Epidemic Algorithms for Replicated Database Maintenance — Demers et al. (PODC 1987)"
    url: "https://dl.acm.org/doi/10.1145/41840.41841"
  - title: "Distributed Systems (4th ed.), van Steen & Tanenbaum — §4.4 Gossip-Based Communication"
    url: "https://www.distributed-systems.net/index.php/books/ds4/"
  - title: "Gossip and Epidemic Protocols — Alberto Montresor (survey, 2017)"
    url: "http://disi.unitn.it/~montreso/ds/papers/montresor17.pdf"
  - title: "Epidemic Protocols (UMass CS677 lecture notes)"
    url: "https://lass.cs.umass.edu/~shenoy/courses/spring13/lectures/Lec17.pdf"
---

"Gossip" gets thrown around as if it means "nodes talk to each other randomly and somehow it works." It's more precise than that. Van Steen groups these under *epidemic protocols*: the goal is to spread information across a large set of nodes the way a disease spreads through a population, and the math of epidemics is what gives you the convergence guarantees. The foundational paper is Demers et al., *Epidemic Algorithms for Replicated Database Maintenance* (Xerox PARC, 1987), which split the design space into two techniques that are still the ones you choose between today.

## Anti-entropy: eventually, everyone matches

In an anti-entropy round, each node periodically picks a random peer and reconciles state with it. "Reconcile" means one of three exchange modes:

- **Push** — I send you my updates.
- **Pull** — I ask you for yours.
- **Push-pull** — we exchange both directions.

The name is the promise: run this long enough and *entropy* (divergence between replicas) goes to zero. Demers proved that with push-pull, the number of nodes still ignorant of an update shrinks *super-exponentially* per round — practically, everyone converges in O(log N) rounds. Pull is faster than push once an update is already widespread, because a healthy node is likely to contact someone who has it. That's why push-pull is the default: push wins the early phase, pull wins the late phase.

The cost is that anti-entropy never stops. Every round, even when there's nothing new, nodes still compare state. To make the comparison cheap you don't ship the whole dataset — you ship a digest (a checksum, or a Merkle tree, covered in an earlier article here) and only transfer the parts that differ.

## Rumor mongering: fast, but it can miss

Anti-entropy's weakness is that "compare everything, every round" is wasteful when updates are rare. Rumor mongering (a.k.a. rumor spreading) fixes that: when a node learns a new update it becomes *infective* and actively pushes the "hot rumor" to random peers. But — mirroring how people stop repeating old news — a node stops spreading a rumor once it keeps contacting peers who already know it (it becomes *removed*).

That stopping rule is what makes rumor mongering cheap, and also what makes it *not* a guarantee. There's a nonzero probability a rumor dies out before reaching everyone. Demers' answer is the pairing you see in real systems: run rumor mongering for speed, and run a slow background anti-entropy sweep to catch the stragglers. Rumor mongering gets the update to 99% of nodes in a blink; anti-entropy guarantees the last 1% eventually get it too.

## A push-pull anti-entropy round

The whole loop is small. Each node, on a timer, picks a random peer and merges. Here the "state" is a set of versioned keys; merge keeps the higher version.

```python
import random

class Node:
    def __init__(self, peers):
        self.peers = peers          # list of other Node references
        self.store = {}             # key -> (version, value)

    def digest(self):
        return {k: v[0] for k, v in self.store.items()}   # key -> version only

    def merge(self, incoming):      # incoming: key -> (version, value)
        for k, (ver, val) in incoming.items():
            if k not in self.store or ver > self.store[k][0]:
                self.store[k] = (ver, val)

    def anti_entropy_round(self):
        peer = random.choice(self.peers)
        # PUSH: send peer everything it's behind on (by comparing digests)
        peer_dig = peer.digest()
        newer = {k: v for k, v in self.store.items()
                 if k not in peer_dig or v[0] > peer_dig[k]}
        peer.merge(newer)
        # PULL: ask peer for anything I'm behind on
        my_dig = self.digest()
        wanted = {k: v for k, v in peer.store.items()
                  if k not in my_dig or v[0] > my_dig[k]}
        self.merge(wanted)
```

Call `anti_entropy_round()` on every node once per tick and watch a single write on one node ripple out. Because peer selection is random and the merge is commutative and idempotent, order doesn't matter and duplicate deliveries are harmless — the same properties that make gossip robust to node churn and message loss.

## Where you've already met this

Anti-entropy is how Dynamo-style stores (and Cassandra, Riak) keep replicas consistent in the background. Rumor-style dissemination is how membership/failure-detection protocols like SWIM spread "node X is dead" quickly. The trade is always the same: anti-entropy costs steady bandwidth for a convergence *guarantee*; rumor mongering costs almost nothing but only gives you *high probability*. Most production systems run both.

**Try next:** Take the snippet above, build a ring of 100 nodes, write one key on node 0, and count how many rounds until all 100 hold it — then switch peer selection from uniform-random to "only my two ring neighbors" and watch convergence collapse from O(log N) to O(N). That gap is exactly why gossip picks peers *randomly*.
