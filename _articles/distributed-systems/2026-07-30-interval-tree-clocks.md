---
title: "Interval Tree Clocks: Causality Tracking That Grows and Shrinks With Your Cluster"
date: 2026-07-30
track: distributed-systems
summary: "Vector clocks need a stable, known set of participant IDs — exactly what you don't have when nodes join and leave constantly. Interval Tree Clocks make the ID space itself divisible, so a node can fork a fresh identity locally and hand it back on exit, with no global registry and no monotonically growing metadata."
reading_time: 6
tags: [interval-tree-clocks, causality, logical-clocks, vector-clocks, dynamic-membership, distributed-systems]
sources:
  - title: "Interval Tree Clocks: A Logical Clock for Dynamic Systems (Almeida, Baquero, Fonte — OPODIS 2008, pp. 259–274, DOI 10.1007/978-3-540-92221-6_18)"
    url: "https://gsd.di.uminho.pt/members/cbm/ps/itc2008.pdf"
  - title: "Interval Tree Clocks — Springer (OPODIS 2008 proceedings)"
    url: "https://link.springer.com/chapter/10.1007/978-3-540-92221-6_18"
  - title: "Interval Tree Clocks — Fred Hébert (ferd.ca)"
    url: "https://ferd.ca/interval-tree-clocks.html"
  - title: "A short introduction to Interval Tree Clocks — Nicolas Seriot (Separate Concerns)"
    url: "https://blog.separateconcerns.com/2017-05-07-itc.html"
  - title: "Interval-Tree-Clocks reference implementation (Java, C, Erlang) — Ricardo Gonçalves"
    url: "https://github.com/ricardobcl/Interval-Tree-Clocks"
---

Vector clocks are the default answer for "did event A happen before B, or are they concurrent?" A vector clock is a map from node ID to a counter, and comparison is pointwise: `A ≤ B` iff every entry of A is `≤` the matching entry of B. It works, but it has a structural assumption baked in — you need a stable, agreed-upon set of node IDs. In a system where replicas are spun up per request, containers churn, or mobile clients appear and vanish, that assumption falls apart. Either you coordinate to assign IDs (a synchronization point you were trying to avoid), or you let the ID set grow forever and never reclaim entries for departed nodes.

Interval Tree Clocks (ITC), from Almeida, Baquero and Fonte at OPODIS 2008, solve this by making the identity space *divisible*. Instead of a globally-registered slot per node, each node owns a slice of the interval `[0, 1)`, and any node can split its own slice in two without asking anyone. The representation grows when membership grows and shrinks back down when nodes merge out — the size tracks the number of *active* participants, not the number that ever existed.

## The model: a stamp is (id, event)

A stamp is a pair `(i, e)`. Think of both components as functions over the continuous interval `[0, 1)`, encoded compactly as binary trees.

- The **id** `i` marks which part of the interval this node is allowed to write in. It maps each point to `{0, 1}`. An id tree is `0`, `1`, or `(left, right)` where each child is itself an id tree. `1` means "I own this whole subinterval," `0` means "not mine."
- The **event** `e` records how much causal history is known across the interval. It maps each point to a non-negative integer (a "height"). An event tree is a leaf integer `n`, or a node `(n, left, right)` meaning "base height `n` everywhere here, plus whatever the children add on their halves."

Comparison is the pointwise rule you already know, lifted to the interval: `(i1, e1) ≤ (i2, e2)` iff `e1(x) ≤ e2(x)` for all `x`. If neither stamp dominates, they are concurrent. Note the id plays no part in comparison — only the event heights do. The id purely governs *who may increment where*.

## Fork, event, join

Three operations drive everything:

- **fork** clones a stamp's causal past and splits its id in two: `fork(i, e) = ((i₁, e), (i₂, e))` with `i₁ + i₂ = i` and `i₁`, `i₂` disjoint. Both children keep an identical copy of `e`; they get non-overlapping halves of the interval to write in. This is how a node joins — an existing member forks locally and hands one stamp to the newcomer. No registry.
- **event** inflates the event component in the region the id owns, advancing the partial order. First it tries **fill** (raise heights into space the id already covers, which can *simplify* the tree, sometimes collapsing it back to a plain integer). If fill can't record anything new, it falls back to **grow**, which adds a level in the owned subtree.
- **join** merges two stamps: `join((i₁, e₁), (i₂, e₂)) = (i₁ + i₂, e₁ ⊔ e₂)`, where `⊔` is the pointwise max of the two event trees, followed by normalization. This is how a node leaves — it joins its stamp into a peer, surrendering its slice of the interval, which the peer reabsorbs.

The seed stamp is `(1, 0)`: owns the whole interval, knows nothing yet. Two derived forms matter in practice. A **peek** is a fork variant that copies only the event component and gives the copy a *null* id `(0, e)` — an **anonymous stamp** that carries causal information but cannot register events. That's exactly what you attach to a message or a replicated value: it participates in ordering but claims no identity.

## A worked example

Start from the seed and fork it into A and B:

```
seed         = (1 ; 0)
fork(seed)   = A=((1,0) ; 0)    B=((0,1) ; 0)
```

A owns the left half of the interval, B the right half; both know height 0 everywhere. Now each records one local event. Neither owns the whole interval, so `event` grows a level in its own half:

```
event(A) = ((1,0) ; (0,1,0))    # base 0, +1 on the left half, 0 on the right
event(B) = ((0,1) ; (0,0,1))    # base 0, 0 on the left, +1 on the right
```

Compare them: on the left half A is at height 1 and B at 0; on the right half B is at 1 and A at 0. Neither dominates — **concurrent**, correctly, since the two events happened independently. Now B leaves and joins into A. Ids recombine to the full interval; events take the pointwise max, then normalize:

```
join(A, B):
  id    = (1,0) + (0,1) = 1
  event = max((0,1,0), (0,0,1)) = (0,1,1)  →  normalize  →  1
  result = (1 ; 1)
```

Both halves reached height 1, so `(0,1,1)` collapses to the leaf `1`: "height 1 is known everywhere." The merged stamp dominates both inputs, and the representation is back to a single integer — the tree shrank as membership shrank. That collapse is the property vector clocks can't give you: departed participants leave no residue.

## What you can actually build with the core

Fork, join and id arithmetic are short and exact. Here's the load-bearing part in Python — the identity splitting and the event-tree merge that make membership dynamic:

```python
# id:     0 | 1 | (id, id)
# event:  int | (int, event, event)

def norm_id(i):
    if i == (0, 0): return 0
    if i == (1, 1): return 1
    return i

def split(i):                       # divide an id into two disjoint halves
    if i == 0: return (0, 0)
    if i == 1: return ((1, 0), (0, 1))
    l, r = i
    if l == 0: a, b = split(r); return ((0, a), (0, b))
    if r == 0: a, b = split(l); return ((a, 0), (b, 0))
    return ((l, 0), (0, r))

def sum_id(a, b):                   # recombine ids on join / node exit
    if a == 0: return b
    if b == 0: return a
    return norm_id((sum_id(a[0], b[0]), sum_id(a[1], b[1])))

def fork(stamp):
    i, e = stamp; l, r = split(i)
    return (l, e), (r, e)

def _node(e):    return (e, 0, 0) if isinstance(e, int) else e
def _lift(e, m): return e + m if isinstance(e, int) else (e[0] + m, e[1], e[2])
def _min(e):     return e if isinstance(e, int) else e[0] + min(_min(e[1]), _min(e[2]))

def _norm_ev(e):
    if isinstance(e, int): return e
    n, l, r = e
    if l == r and isinstance(l, int): return n + l      # both halves equal -> collapse
    m = min(_min(l), _min(r))
    return (n + m, _lift(l, -m), _lift(r, -m))

def join_ev(a, b):                  # pointwise max of two event trees
    if isinstance(a, int) and isinstance(b, int): return max(a, b)
    na, la, ra = _node(a); nb, lb, rb = _node(b)
    if na > nb: return join_ev(b, a)
    d = nb - na
    return _norm_ev((na, join_ev(la, _lift(lb, d)), join_ev(ra, _lift(rb, d))))

def join(s1, s2):
    return (sum_id(s1[0], s2[0]), join_ev(s1[1], s2[1]))
```

Wire this behind a KV store the way Riak-style systems use dotted version vectors: keep the divisible id per node, keep a per-value event component, attach an anonymous (`peek`) stamp to each replicated write, and use pointwise `≤` to detect conflicting concurrent updates. Because the id is stored and split separately from the data, a node splitting its key space doesn't force you to rewrite every value it owns — the split is cheap and local, which is the whole point in a churning cluster.

**Try next:** implement `event` (fill-then-grow) and a `leq` comparison on top of the code above, then run a fuzz test — random sequences of fork/event/join across N simulated nodes — asserting that ITC's happens-before verdicts match a reference vector clock on the same event graph, while logging the serialized byte size of each so you can watch ITC stay flat as nodes join and leave versus the vector clock's monotonic growth.
