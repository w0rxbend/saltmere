---
title: "The consistent hash ring: move a node, remap only K/N keys"
date: 2026-07-25
track: distributed-systems
summary: "Modulo hashing remaps almost every key when the cluster resizes. A hash ring with virtual nodes remaps only ~K/N of them. Here's the ring, why the bound holds, and a 30-line Python implementation."
reading_time: 5
tags: [consistent-hashing, dht, sharding, virtual-nodes]
sources:
  - title: "Karger et al., Consistent Hashing and Random Trees (STOC 1997)"
    url: "https://dl.acm.org/doi/10.1145/258533.258660"
  - title: "Karger et al. (1997), free PDF"
    url: "https://www.cs.princeton.edu/courses/archive/fall09/cos518/papers/chash.pdf"
  - title: "van Steen & Tanenbaum, Distributed Systems (4th ed.) — Naming & flat/structured naming"
    url: "https://www.distributed-systems.net/index.php/books/ds4/"
  - title: "DeCandia et al., Dynamo: Amazon's Highly Available Key-value Store (SOSP 2007)"
    url: "https://www.allthingsdistributed.com/files/amazon-dynamo-sosp2007.pdf"
---

You have `K` keys and `N` nodes and you want `key -> node` that stays stable when `N` changes. The obvious answer, `node = hash(key) % N`, is a trap: change `N` and the divisor changes, so almost every key lands somewhere new. Add one node to a 10-node cluster and you reshuffle roughly 90% of your keys — every cache miss, every data-movement, all at once. Karger et al. (1997) fixed this with a hash *ring*, and it's the same structured-naming idea van Steen & Tanenbaum use to locate entities in a DHT: hash the name into a circular id space and let position decide ownership.

## The ring

Hash both keys and nodes into the same space — say the 128-bit output of a hash function, wrapped into a circle at `2^128`. A key is owned by the first node you meet walking clockwise from the key's position. That's the whole rule.

Now delete a node. Only the keys that fell in *that node's* arc move — they slide clockwise to the next node. Every other key keeps its owner, because their nearest-clockwise node didn't change. Add a node and the mirror happens: it steals just the arc between itself and its counter-clockwise neighbor. Either way you touch one node's worth of keys, which is on average `K/N` — the property the paper calls **monotonicity**: items only ever move *to* the new bucket, never churn between existing ones.

## Virtual nodes, because one arc per node is lumpy

With one point per node, arc sizes are random and uneven — some node gets a 2x slice and a hotspot. Worse, when a node leaves, *all* its load dumps onto a single successor. The fix is **virtual nodes**: hash each physical node to `V` positions on the ring (`node#0`, `node#1`, …). Load smooths out as `1/sqrt(V)`, so a few hundred vnodes per node gets you within a few percent of even. And when a node dies, its `V` little arcs spill onto `V` *different* successors instead of crushing one. Dynamo runs exactly this ring with vnodes; van Steen & Tanenbaum frame the same trick as balancing the key space across a structured overlay.

## Thirty lines that actually work

`bisect` on a sorted list of ring positions turns "walk clockwise to the next node" into a binary search:

```python
import bisect, hashlib

class HashRing:
    def __init__(self, nodes=None, vnodes=150):
        self.vnodes = vnodes
        self._keys = []            # sorted ring positions
        self._ring = {}            # position -> physical node
        for n in nodes or []:
            self.add(n)

    def _hash(self, key):
        return int(hashlib.md5(key.encode()).hexdigest(), 16)

    def add(self, node):
        for i in range(self.vnodes):
            h = self._hash(f"{node}#{i}")
            self._ring[h] = node
            bisect.insort(self._keys, h)     # keep positions sorted

    def remove(self, node):
        for i in range(self.vnodes):
            h = self._hash(f"{node}#{i}")
            del self._ring[h]
            self._keys.remove(h)

    def get(self, key):
        if not self._keys:
            return None
        h = self._hash(key)
        idx = bisect.bisect(self._keys, h) % len(self._keys)   # wrap the ring
        return self._ring[self._keys[idx]]
```

The one subtle line is `bisect.bisect(...) % len(...)`: `bisect` returns the insertion point *after* equal elements, and the modulo wraps a key past the last position back to the first node — closing the circle.

## Prove the K/N bound to yourself

Map 100k keys over 10 nodes, snapshot the assignment, add an 11th node, and diff:

```python
r = HashRing([f"node{i}" for i in range(10)])
keys = [f"key:{i}" for i in range(100_000)]
before = {k: r.get(k) for k in keys}
r.add("node10")
moved = sum(before[k] != r.get(k) for k in keys)
print(moved / len(keys))     # ~0.09, i.e. ≈ 1/11
```

You'll see roughly 9% move — about `1/(N+1)`. Swap the ring for `hash(key) % N` and the same experiment moves ~90%. That gap is the entire reason DHTs, sharded caches, and Cassandra/Dynamo partitioners are built on rings.

**Try next:** extend `get()` to return the next `R` *distinct* physical nodes clockwise instead of one — that's the preference list Dynamo replicates each key onto, and it wires this ring straight into last article's quorum `(N, R, W)`.
