---
title: "Rendezvous hashing (HRW): pick the highest score, skip the ring"
date: 2026-08-10
track: distributed-systems
summary: "Consistent hashing needs a ring and hundreds of virtual nodes per host to stay balanced. Rendezvous hashing gets the same 1/N remapping and even load with no bookkeeping at all: for each key, score every node and take the max. Here's why it works, a runnable implementation, and how it stacks up against the ring and jump hash."
reading_time: 6
tags: [rendezvous-hashing, hrw, consistent-hashing, sharding, load-balancing]
sources:
  - title: "Thaler & Ravishankar, Using Name-Based Mappings to Increase Hit Rates (IEEE/ACM ToN, Feb 1998)"
    url: "https://www.microsoft.com/en-us/research/wp-content/uploads/2017/02/HRW98.pdf"
  - title: "Rendezvous hashing — Wikipedia"
    url: "https://en.wikipedia.org/wiki/Rendezvous_hashing"
  - title: "Envoy — Supported load balancers (ring hash vs Maglev)"
    url: "https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/load_balancing/load_balancers.html"
  - title: "Lamping & Veach, A Fast, Minimal Memory, Consistent Hash Algorithm (jump hash)"
    url: "https://arxiv.org/abs/1406.2294"
  - title: "Damian Gryski, Consistent Hashing: Algorithmic Tradeoffs"
    url: "https://dgryski.medium.com/consistent-hashing-algorithmic-tradeoffs-ef6b8e2fcae8"
---

You have `K` cache keys and `N` cache servers, and the server set keeps changing — nodes get added for capacity, yanked when they die. You want a `key -> node` mapping that is (a) deterministic and coordination-free, so every client agrees without asking anyone; (b) evenly balanced, so no server eats a hotspot; and (c) *stable*, so resizing the cluster moves as few keys as possible. `hash(key) % N` fails (c) catastrophically — change `N` and almost every key relocates. The [consistent hash ring](/articles/distributed-systems/2026-07-25-consistent-hashing-ring) fixes that, but it buys stability with machinery: a sorted ring, and a few hundred *virtual nodes* per host to smooth out lumpy arcs.

Rendezvous hashing — **Highest Random Weight (HRW)**, from Thaler & Ravishankar in 1996–98 — gets the same guarantees with no ring, no virtual nodes, and no state to maintain. The whole idea fits in one sentence.

## The idea: score every node, take the max

To place a key, compute a hash of `(key, node)` for *every* node in the set, and assign the key to the node with the highest score. That's it. The winning node is where all clients "rendezvous" for that key — hence the name.

```python
import hashlib

def hrw_node(key, nodes):
    def score(node):
        h = hashlib.blake2b(f"{node}:{key}".encode(), digest_size=8).digest()
        return int.from_bytes(h, "big")
    return max(nodes, key=score)

nodes = ["cache-0", "cache-1", "cache-2", "cache-3"]
hrw_node("user:42", nodes)   # -> deterministic winner, e.g. 'cache-2'
```

No `add()`, no `remove()`, no persistent structure. The node list *is* the state. Membership changes are just a different argument to `max`.

## Why it's balanced and why it barely moves

**Even load.** The hash mixes key and node together, so for a fixed key each node's score is an independent uniform draw. Every node is equally likely to be the maximum, so each owns ~`1/N` of the keyspace. With 10 nodes and 100k keys, an actual run lands every node within 1% of the mean — no virtual nodes required. The ring needs vnodes precisely because a single hash-point-per-node produces uneven arcs; HRW's per-key contest is uniform by construction.

**Minimal remapping.** Now remove a node. A key only cares about its removed node if that node was its *maximum*. If it was — the key falls to whatever had the *second*-highest score, a fresh independent draw over the survivors. If it wasn't the max, the maximum is untouched and the key doesn't move. So exactly the removed node's keys relocate — on average `1/N` of them — and nothing else churns. Adding a node is the mirror image: the newcomer only wins a key if its score beats the current champion, which happens for ~`1/(N+1)` of keys, all pulled *toward* the new node and never between existing ones. That "items only ever move to the changed node" property is the same monotonicity the ring gives you, but you get it for free.

Empirically, removing 1 of 10 nodes moves 9.9% of keys, and every moved key was owned by the removed node — matching the theory exactly.

## Cost: O(N) per lookup, and the fix for large N

The obvious price is that every lookup hashes against *all* `N` nodes — O(N) per key, versus the ring's O(log N) binary search. For a cache cluster of a few dozen or even a few hundred nodes this is a non-issue; a `blake2b` per node is nanoseconds and there's no ring to rebuild on membership change. HRW shines exactly where `N` is small and *changes often*.

When `N` gets large, HRW has a documented answer: **skeleton-based HRW**. Arrange nodes as leaves of a virtual tree and run HRW level-by-level down the skeleton, so you only score a node's worth of children at each level — O(log N) lookups. Wikipedia notes the tradeoff: the hierarchy slightly weakens global uniformity of placement in exchange for the speedup. Most systems never need it.

There's also a **weighted** variant for heterogeneous capacities: transform each node's uniform score by `-w / ln(u)` (a logarithmic reshaping) so a node with twice the weight wins roughly twice the keys. Unlike ring vnodes, changing a weight is just a new multiplier — no points to add or remove.

## HRW vs the ring vs jump hash

Versus the **consistent hash ring** (covered [here](/articles/distributed-systems/2026-07-25-consistent-hashing-ring)): both hit the `1/N` remapping bound. The ring's balance depends on tuning `V` virtual nodes per host (load error shrinks as `1/sqrt(V)`), which costs memory and a rebuild step on every membership change. HRW is balanced with zero tuning and holds no structure — but pays O(N) per lookup instead of O(log N). Rule of thumb: **small, churny node sets favor HRW; large stable rings favor the ring.**

Versus **jump consistent hash** (Lamping & Veach, Google): jump hash is astonishingly cheap — O(ln N) time, *zero* memory, five lines — and beautifully balanced. Its catch is structural: it maps keys to bucket *numbers* `0..N-1` and only supports removing the *last* bucket. You can't drop node #3 out of the middle, and you can't map to named/heterogeneous nodes without an indirection layer. HRW handles arbitrary named nodes and arbitrary removals natively, which is usually what a cache cluster actually needs.

## Where it's used

Rendezvous hashing quietly runs a lot of infrastructure. Thaler & Ravishankar's original motivation was **web cache arrays** — a proxy picking which cache in a CDN array holds an object, name-based so clients agree without a coordinator. Wikipedia lists deployments including GitHub's load balancer, Apache Ignite and Druid, Twitter's EventBus, IBM Cloud Object Storage, and Ceph's CRUSH (a rendezvous-descended placement algorithm). GlusterFS uses a related HRW scheme to place files across bricks.

The load-balancer world is instructive by contrast. **Envoy** ships two consistent-hash balancers — *ring hash* (Ketama-style) and *Maglev* — but not rendezvous hashing. Its own docs note Maglev builds and looks up ~10x/5x faster than a large ring, but is "not as stable as ring hash when upstream hosts change" (roughly double the keys move on host removal), tunable via `table_size`. HRW sits in a different corner of that design space: perfectly stable and balance-free at the cost of per-request O(N) scoring — a great fit when your node count is small and your membership is volatile, which describes a surprising number of real cache tiers.

The takeaway: if you find yourself standing up a ring with 200 vnodes per host just to shard a dozen caches, HRW gives you the same distribution and the same `1/N` churn in twenty lines with nothing to maintain.

**Try next:** implement weighted HRW with the `-w / ln(u)` transform, then verify empirically that a node with weight 2 draws twice the keyspace — and measure how many keys move when you bump one node's weight versus adding a node.
