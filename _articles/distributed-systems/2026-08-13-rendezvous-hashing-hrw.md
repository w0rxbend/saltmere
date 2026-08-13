---
title: "Rendezvous hashing (HRW): pick the node with the highest score, skip the ring"
date: 2026-08-13
track: distributed-systems
summary: "How Highest Random Weight hashing assigns keys by hashing (key, node) and taking the max — no ring, no virtual nodes, provably minimal remapping on membership change — and where it beats and loses to consistent hashing."
reading_time: 5
tags: [rendezvous-hashing, hrw, consistent-hashing, sharding, load-balancing]
sources:
  - title: "Thaler & Ravishankar — A Name-Based Mapping Scheme for Rendezvous (Univ. Michigan CSE-TR-316-96, 1996)"
    url: "https://www.microsoft.com/en-us/research/wp-content/uploads/2017/02/HRW98.pdf"
  - title: "Thaler & Ravishankar — Using Name-Based Mappings to Increase Hit Rates (IEEE/ACM Trans. Networking, Feb 1998)"
    url: "https://www.semanticscholar.org/paper/Using-name-based-mappings-to-increase-hit-rates-Thaler-Ravishankar/6a3d10bb30818c86c18cef1e5e4b128ae80840ae"
  - title: "Rendezvous hashing (Wikipedia) — algorithm, O(n) cost, weighted variant"
    url: "https://en.wikipedia.org/wiki/Rendezvous_hashing"
  - title: "Consistent Hashing vs. Rendezvous Hashing: A Comparison (DZone)"
    url: "https://dzone.com/articles/consistent-hashing-vs-rendezvous-hashing-a-compara"
---

Consistent hashing solved the "N mod servers" resharding problem by placing nodes and keys on a ring, but it costs you a data structure: a sorted ring of virtual nodes you have to build, balance, and search. **Rendezvous hashing** (Thaler & Ravishankar, 1996) gets the same minimal-remapping guarantee with no ring at all. The idea is almost too simple: to place a key, hash it *together with each node*, and send it to the node with the highest score.

## The algorithm

For a key `k` and node set `N`, compute `score = hash(k, node)` for every node and pick the argmax. That's the entire "highest random weight" rule.

```python
import hashlib

def hrw_node(key: str, nodes: list[str]) -> str:
    def score(node: str) -> int:
        h = hashlib.blake2b(f"{key}:{node}".encode(), digest_size=8)
        return int.from_bytes(h.digest(), "big")
    return max(nodes, key=score)   # deterministic, no shared state
```

Because `hash` mixes both inputs, each node's score for a given key is effectively an independent uniform random value, so keys spread evenly across nodes — no virtual nodes needed to fix the lumpy-arc problem consistent hashing has. Every client computes the same answer from just the key and the current node list; there's no ring to replicate.

## Why remapping is minimal

Remove node `X`. A key only moves if `X` was its winner — and then it moves to whoever had the *second*-highest score, while every key that didn't point at `X` keeps its winner untouched. With `n` nodes each key lands on `X` with probability `1/n`, so removing one node remaps exactly ~`1/n` of keys, the information-theoretic minimum. Adding a node `Y` is the mirror image: a key moves to `Y` only if `Y` now outscores its old winner, again ~`1/n` of keys. No mass reshuffle, and crucially **no dependence on neighbor placement** the way a ring has.

## HRW vs consistent hashing

| | Consistent hashing | Rendezvous (HRW) |
|---|---|---|
| Lookup cost | O(log V) binary search on ring | O(n) hashes, one per node |
| Extra structure | Ring of V virtual nodes per server | None — just the node list |
| Even load | Needs 100–200 vnodes/server to balance | Uniform by construction |
| Remap on change | ~1/n of keys | ~1/n of keys (provably minimal) |
| Top-k / replicas | Walk ring clockwise | Take top-k scores directly |
| Weighting | Assign vnode counts | Closed-form weighted score |

The trade is right there: HRW is **O(n) per lookup** vs consistent hashing's **O(log V)**. For a few dozen nodes, computing n hashes is trivial and often faster than a ring search plus the memory of thousands of vnodes. At thousands of nodes, the linear scan bites and consistent hashing (or skeleton/hierarchical HRW, which restores O(log n)) wins.

## Weighting and replica sets

Uneven node capacities are handled without vnodes. Map the raw hash into `(0,1]`, then use a closed-form weighted score so win probability is proportional to `weight`:

```python
import math
def weighted_score(key, node, weight):
    u = (int.from_bytes(hashlib.blake2b(f"{key}:{node}".encode(),
                        digest_size=8).digest(), "big") + 1) / 2**64
    return weight / -math.log(u)   # win prob ∝ weight
```

And replication is free: instead of the single argmax, take the **top-k** nodes by score. Those k are the natural replica set, and if one leaves, only its share of keys shifts to the next-highest node — the k-th replica each key gains is exactly the (k+1)-th ranked node it already knew about.

This is why HRW quietly shows up in caching meshes, GLB backends, and shard routers: no shared ring to keep consistent across clients, top-k replicas for free, and remapping you can prove is minimal on a whiteboard.

**Try next:** implement `hrw_node`, hash 100k keys over 10 nodes and record the assignment, then drop node 5 and re-hash. Count how many keys moved — it should be within noise of 1/10, and every moved key should have pointed at node 5.
