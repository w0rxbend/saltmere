---
title: "Quorum replication: why R + W > N is the whole game"
date: 2026-07-25
track: distributed-systems
summary: "Vector clocks tell you a conflict happened. Quorums stop most conflicts from happening in the first place — with a single inequality you can tune per request. Here's the rule, the arithmetic, and a 30-line simulation."
reading_time: 5
tags: [replication, quorum, consistency, availability, van-steen]
sources:
  - title: "van Steen & Tanenbaum, Distributed Systems (4th ed.), §7.5 Replication protocols"
    url: "https://www.distributed-systems.net/index.php/books/ds4/"
  - title: "Gifford, Weighted Voting for Replicated Data (SOSP 1979)"
    url: "https://dl.acm.org/doi/10.1145/800215.806583"
  - title: "DeCandia et al., Dynamo: Amazon's Highly Available Key-value Store (SOSP 2007)"
    url: "https://www.allthingsdistributed.com/files/amazon-dynamo-sosp2007.pdf"
---

Once you can *detect* a write-write conflict with vector clocks, the next question is how to *avoid* most of them without falling back to a global lock. Chapter 7 of van Steen & Tanenbaum gives the answer that Dynamo, Cassandra, and Riak all ship: quorum-based replication. The entire mechanism is one inequality you get to tune per request.

## The rule

Store every object on `N` replicas. On each operation, don't talk to all of them — talk to a *quorum*:

- a write must be acknowledged by `W` replicas,
- a read must collect responses from `R` replicas.

Pick `W` and `R` so that:

```
R + W > N      # read quorum and write quorum always overlap
W > N / 2      # two writes can never both win in disjoint sets
```

The first line is the one that matters most. If `R + W > N`, then any read set and any write set share at least one replica — by pigeonhole, they *cannot* be disjoint. So a read is guaranteed to touch at least one replica that saw the latest committed write. Attach a version (a vector clock or a plain counter) to each copy and the reader just keeps the newest one. That is how you get read-your-writes without contacting every node.

## The arithmetic is the design knob

The same inequality gives you a slider between latency and consistency, and you can move it *per call*:

| N | W | R | Behavior |
|---|---|---|----------|
| 3 | 2 | 2 | Balanced. Tolerates 1 node down for both reads and writes. |
| 3 | 3 | 1 | Fast reads, `ROWA` writes — but any node down blocks writes. |
| 3 | 1 | 3 | Fast writes, slow reads. Write survives if 2 nodes are down. |
| 3 | 1 | 1 | `R + W = 2 ≯ 3`: **not** a strict quorum. Fast, eventually consistent, may read stale. |

That last row is the important one: it's a legal, useful configuration, it just doesn't satisfy `R + W > N`, so it gives up the overlap guarantee in exchange for the lowest possible latency and highest availability. Dynamo exposes exactly these `(N, R, W)` knobs so each workload picks its own point on the curve — shopping carts favor availability, so they run low `W`; a config service that must never read stale runs `R + W > N`.

## Prove it to yourself in 30 lines

Simulate replicas as a list of `(value, version)` and check the overlap property directly:

```python
import random

N = 5
replicas = [(None, 0)] * N          # (value, version) per replica

def write(value, W):
    version = max(v for _, v in replicas) + 1
    targets = random.sample(range(N), W)      # any W replicas ack
    for i in targets:
        replicas[i] = (value, version)
    return set(targets)

def read(R):
    responders = random.sample(range(N), R)   # any R replicas answer
    latest = max((replicas[i] for i in responders), key=lambda x: x[1])
    return latest, set(responders)

w_set = write("v2", W=3)
(val, ver), r_set = read(R=3)          # R + W = 6 > N = 5
assert w_set & r_set, "quorums must overlap!"   # never fires
print(val, "read from a quorum overlapping the write:", w_set & r_set)
```

Run it in a loop with `W=3, R=3` and the assertion never fires — every read set intersects the write set. Now drop to `W=2, R=2` (`R + W = 4 ≯ 5`) and it fails within a handful of iterations: you've reproduced a stale read. Being able to *cause* the stale read on demand is what makes the inequality stop feeling like trivia.

## Where it bites

Strict quorums are not linearizability. Concurrent writes can still produce siblings (that's why you kept the version), a coordinator crash mid-write can leave `W` partially applied, and "sloppy quorums" with hinted handoff — what Dynamo actually runs — relax the *membership* of the quorum during partitions, trading the clean overlap proof for staying writable. The clean rule is the mental model; production is the rule plus a pile of caveats.

**Try next:** extend the simulation to store a vector clock per replica instead of an integer version, run two concurrent writes at `W=2, N=3`, and detect the resulting siblings on read. You'll have wired quorums and last article's vector clocks into one working replica.
