---
title: "Interval Tree Clocks: Causality Tracking That Grows and Shrinks With the Cluster"
date: 2026-07-30
track: distributed-systems
summary: "Vector clocks require a stable, known set of participant identifiers, which dynamic membership does not provide. Interval Tree Clocks make the identifier space itself divisible, so a node forks a fresh identity locally and surrenders it on exit, with no global registry and no monotonically growing metadata."
reading_time: 7
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

**Gist.** A vector clock is a map from node identifier to counter, so it presumes an agreed, stable identifier set; under churn that set must either be assigned by coordination or grow without reclamation. Interval Tree Clocks (ITC), introduced by Almeida, Baquero and Fonte at OPODIS 2008, replace registered slots with a *divisible* identity: each participant owns a slice of the interval `[0, 1)` and may split its own slice locally, with no registry. The cost is that both components of the clock are trees rather than flat maps — every operation is a recursive tree walk, and the representation stays compact only when departing participants join their stamps back into a peer.

## The model: a stamp is (id, event)

A stamp is a pair `(i, e)`. Both components are functions over the continuous interval `[0, 1)`, encoded compactly as binary trees.

- The **id** `i` marks which part of the interval this node is permitted to write in. It maps each point to `{0, 1}`. An id tree is `0`, `1`, or `(left, right)` where each child is itself an id tree. `1` denotes ownership of the whole subinterval, `0` denotes no ownership.
- The **event** `e` records how much causal history is known across the interval. It maps each point to a non-negative integer height. An event tree is a leaf integer `n`, or a node `(n, left, right)` meaning base height `n` throughout, plus whatever the children add on their halves.

Comparison is the pointwise rule familiar from vector clocks, lifted to the interval: `(i1, e1) ≤ (i2, e2)` iff `e1(x) ≤ e2(x)` for all `x`. If neither stamp dominates, they are concurrent. **The id takes no part in comparison** — only the event heights do. The id governs solely who may increment where.

The load-bearing invariant is that **the ids held by all live stamps are pairwise disjoint and, taken together, sum to the full interval `1`**. Fork preserves it by splitting, join preserves it by summing. Every operation that is safe in ITC is safe because it maintains this partition.

## Fork, event, join

Three operations drive everything.

- **fork** clones a stamp's causal past and splits its id in two: `fork(i, e) = ((i₁, e), (i₂, e))` with `i₁ + i₂ = i` and `i₁`, `i₂` disjoint. Both children keep an identical copy of `e` and receive non-overlapping halves of the interval to write in. This is how a node joins: an existing member forks locally and hands one stamp to the newcomer.
- **event** inflates the event component within the region the id owns, advancing the partial order. It first attempts **fill**, raising heights into space the id already covers, which can *simplify* the tree and sometimes collapse it back to a plain integer. If fill records nothing new, it falls back to **grow**, which adds a level in the owned subtree.
- **join** merges two stamps: `join((i₁, e₁), (i₂, e₂)) = (i₁ + i₂, e₁ ⊔ e₂)`, where `⊔` is the pointwise maximum of the two event trees, followed by normalization. This is how a node leaves: it joins its stamp into a peer, surrendering its slice of the interval, which the peer reabsorbs.

The seed stamp is `(1, 0)` — owns the whole interval, knows nothing yet. One derived form matters in practice. A **peek** is a fork variant that copies only the event component and gives the copy a *null* id, producing `(0, e)`: an **anonymous stamp** that carries causal information but cannot register events. That is the form attached to a message or a replicated value; it participates in ordering while claiming no identity.

## A worked example

Starting from the seed and forking into A and B:

```
seed         = (1 ; 0)
fork(seed)   = A=((1,0) ; 0)    B=((0,1) ; 0)
```

A owns the left half of the interval, B the right half; both know height 0 everywhere. Each then records one local event. Neither owns the whole interval, so `event` grows a level in its own half:

```
event(A) = ((1,0) ; (0,1,0))    # base 0, +1 on the left half, 0 on the right
event(B) = ((0,1) ; (0,0,1))    # base 0, 0 on the left, +1 on the right
```

On the left half A is at height 1 and B at 0; on the right half B is at 1 and A at 0. Neither dominates, so the verdict is **concurrent**, which is correct: the two events happened independently. B then leaves and joins into A. Ids recombine to the full interval; events take the pointwise maximum, then normalize:

```
join(A, B):
  id    = (1,0) + (0,1) = 1
  event = max((0,1,0), (0,0,1)) = (0,1,1)  →  normalize  →  1
  result = (1 ; 1)
```

Both halves reached height 1, so `(0,1,1)` collapses to the leaf `1`: height 1 is known everywhere. The merged stamp dominates both inputs and the representation is back to a single integer — **the tree shrank as membership shrank**. That collapse is the property a vector clock cannot offer: a departed participant leaves no residue.

### Implementation sketch (Scala)

Identity splitting and the event-tree merge are the parts that make membership dynamic. `event` (fill-then-grow) and the `leq` comparison are omitted.

The id half. `split` is `fork`'s identity rule, `sumId` is `join`'s; `normId` is what lets a reabsorbed slice disappear from the representation.

```scala
enum Id:
  case Zero, One
  case Node(l: Id, r: Id)

import Id.*

def normId(i: Id): Id = i match
  case Node(Zero, Zero) => Zero
  case Node(One, One)   => One
  case other            => other

def split(i: Id): (Id, Id) = i match
  case Zero          => (Zero, Zero)
  case One           => (Node(One, Zero), Node(Zero, One))
  case Node(Zero, r) => val (a, b) = split(r); (Node(Zero, a), Node(Zero, b))
  case Node(l, Zero) => val (a, b) = split(l); (Node(a, Zero), Node(b, Zero))
  case Node(l, r)    => (Node(l, Zero), Node(Zero, r))

def sumId(a: Id, b: Id): Id = (a, b) match
  case (Zero, x) => x
  case (x, Zero) => x
  case (Node(al, ar), Node(bl, br)) => normId(Node(sumId(al, bl), sumId(ar, br)))
  case _ => One // unreachable while ids stay disjoint
```

The event half. `joinEv` is the pointwise maximum, `normEv` the collapse that keeps the tree small.

```scala
enum Ev:
  case Leaf(n: Int)
  case Node(n: Int, l: Ev, r: Ev)

import Ev.{Leaf, Node as EN}

def lift(e: Ev, m: Int): Ev = e match
  case Leaf(n)     => Leaf(n + m)
  case EN(n, l, r) => EN(n + m, l, r)

def minEv(e: Ev): Int = e match
  case Leaf(n)     => n
  case EN(n, l, r) => n + math.min(minEv(l), minEv(r))

// factor the common base height out of both halves, or drop them entirely
def normEv(e: Ev): Ev = e match
  case EN(n, Leaf(a), Leaf(b)) if a == b => Leaf(n + a)
  case EN(n, l, r) =>
    val m = math.min(minEv(l), minEv(r))
    EN(n + m, lift(l, -m), lift(r, -m))
  case leaf => leaf

def parts(e: Ev): (Int, Ev, Ev) = e match
  case Leaf(n)     => (n, Leaf(0), Leaf(0))
  case EN(n, l, r) => (n, l, r)

def joinEv(a: Ev, b: Ev): Ev = (a, b) match
  case (Leaf(x), Leaf(y)) => Leaf(math.max(x, y))
  case _ =>
    val (na, la, ra) = parts(a)
    val (nb, lb, rb) = parts(b)
    if na > nb then joinEv(b, a)
    else
      val d = nb - na // rebase b's children onto a's lower base before merging
      normEv(EN(na, joinEv(la, lift(lb, d)), joinEv(ra, lift(rb, d))))
```

The two halves never interact: `split` and `sumId` rewrite only the id, `joinEv` only the event tree. A membership change is therefore a local edit to the id component alone.

## Pitfalls

- **Forking a stamp and discarding one half permanently loses that slice of the interval.** The remaining stamps no longer sum to `1`, and no later join can restore the missing region, so events recorded there by a resurrected copy are unattributable.
- **A node that exits without joining its stamp into a peer leaks its slice.** The interval never recombines, `normId` never collapses the id tree, and the representation retains a branch for a participant that no longer exists.
- **Copying a stamp instead of forking it produces two writers with the same id.** Both increment the same region, so the disjointness invariant is broken and independent updates can compare as ordered rather than concurrent — a lost conflict rather than a detected one.
- **Anonymous stamps `(0, e)` cannot record events.** `event` on a null id has no owned region to inflate; attaching a peeked stamp to a value and then attempting to advance it in place yields no progress.
- **Comparison ignores the id.** Two stamps with disjoint ids and identical event trees compare as equal, which is correct but means the id cannot be used to break ties or identify the writer.
- **Skipping normalization after join lets event trees grow without bound.** The heights remain correct and comparison still returns the right verdict, so the defect surfaces only as steadily increasing serialized size.
