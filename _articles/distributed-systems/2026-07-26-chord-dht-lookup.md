---
title: "Chord: finding any key in O(log N) hops without a directory"
date: 2026-07-26
track: distributed-systems
summary: "The consistent hash ring states who owns a key. Chord states how to locate that owner without every node knowing every other node: finger tables, the find_successor lookup, and the stabilization protocol that preserves correctness while nodes join and leave."
reading_time: 6
tags: [chord, dht, finger-table, p2p, naming, stabilization, distributed-systems]
sources:
  - title: "Stoica, Morris, Karger, Kaashoek, Balakrishnan — Chord: A Scalable Peer-to-peer Lookup Service for Internet Applications (SIGCOMM 2001)"
    url: "https://pdos.csail.mit.edu/papers/chord:sigcomm01/chord_sigcomm.pdf"
  - title: "Stoica et al. — Chord: A Scalable Peer-to-peer Lookup Protocol for Internet Applications (IEEE/ACM ToN, extended version)"
    url: "https://pdos.csail.mit.edu/papers/ton:chord/paper-ton.pdf"
  - title: "Wikipedia — Chord (peer-to-peer)"
    url: "https://en.wikipedia.org/wiki/Chord_(peer-to-peer)"
  - title: "MIT 6.033 (2001) — Chord Implementation handout"
    url: "https://web.mit.edu/6.033/2001/wwwdocs/handouts/dp2-chord.html"
---

**Gist.** The [consistent hash ring](/distributed-systems/2026-07-25-consistent-hashing-ring) resolves ownership by walking clockwise from `hash(key)` to the first node, a rule that presupposes each node already holds the position of every other node. Chord (Stoica, Morris, Karger, Kaashoek, Balakrishnan, SIGCOMM 2001) removes that presupposition: each node keeps **O(log N) pointers** — a finger table whose entries double in reach — and resolves any key in **O(log N) expected hops** by forwarding the query. The cost is that routing state is only eventually correct, so a background stabilization protocol must run continuously, and during convergence a lookup can be slow or, in the window before successor pointers settle, wrong.

## The ring, minimally

Chord hashes both nodes and keys into the same m-bit identifier space (SHA-1, so typically m = 160), arranged as a circle of size 2^m. A key `k` is owned by its **successor**: the first node whose identifier is ≥ `k` going clockwise. Every node stores two pointers into this ring: `successor` (next node clockwise) and `predecessor` (previous node counter-clockwise).

With successor pointers alone, a lookup is a linear walk around the ring: correct, but **O(N) hops**. The finger table converts that walk into a binary search.

## Finger tables

Node `n`'s finger table has up to m entries. The i-th entry points to the first node that succeeds `n + 2^(i-1)` (mod 2^m):

```
finger[i].start  = (n + 2^(i-1)) mod 2^m        for i = 1 .. m
finger[i].node   = successor(finger[i].start)
```

`finger[1]` is therefore the successor. `finger[2]` targets a point twice as far around the ring, `finger[3]` twice as far again, so **each entry doubles the reach of the previous one**. A node with m = 160 stores at most 160 pointers regardless of network size: **O(log N) state per node**, not O(N).

| | flat successor chain | Chord finger table |
|---|---|---|
| State per node | O(1) (successor only) | O(log N) |
| Lookup hops | O(N) | O(log N) expected |
| Join cost (fingers to fix) | — | O(log² N) messages |
| Correctness under churn | breaks if successor is stale | self-heals via stabilization |

## The lookup: find_successor

To resolve key `id`, a node tests whether the key falls in the half-open interval between itself and its immediate successor. If not, the query is not passed to the successor; it jumps to the finger entry that lands **closest to the target without overshooting it**, and that node repeats the procedure:

```
// n.find_successor(id)
if id ∈ (n, successor]:
    return successor
else:
    n' = closest_preceding_node(id)
    return n'.find_successor(id)      // forward the query

// n.closest_preceding_node(id)
for i = m downto 1:
    if finger[i].node ∈ (n, id):
        return finger[i].node
return n
```

The scan runs from the farthest finger inwards, so the chosen hop is the largest jump that still stays behind `id`. **Each hop at least halves the remaining ring distance to the target**, which is the entire argument for O(log N) expected hops — the halving argument of binary search applied to a circle rather than an array. The original paper's simulations agree: the mean lookup path length grows as roughly 0.5 · log₂(N) hops, and the measured latency of a deployment on Internet hosts rises with the node count at the same logarithmic rate.

## Join and leave: three invariants

A joining node `n` needs one contact, some existing node `n'`, to bootstrap:

```
// n.join(n')
predecessor = nil
successor   = n'.find_successor(n)
```

That single `find_successor` call places `n` at its correct ring position. Three invariants must then hold for lookups to remain correct: **(1) every node's successor pointer is correct, (2) every key is stored at its true successor, (3) finger tables are reasonably fresh.** Chord does not repair all three synchronously. Invariant (3) is relaxed and repaired by a background process, which is safe because a stale finger only misroutes a hop, never the final answer — `find_successor` treats the successor pointer, not the fingers, as ground truth.

## Stabilization: eventually-correct successors

Each node periodically runs four routines rather than updating the network atomically at join time:

```
// runs periodically at node n
def stabilize():
    x = successor.predecessor
    if x ∈ (n, successor):
        successor = x                 # a closer successor appeared
    successor.notify(n)

// n.notify(n'):  n' claims to be n's predecessor
def notify(n'):
    if predecessor is nil or n' ∈ (predecessor, n):
        predecessor = n'

def fix_fingers():
    next = (next + 1) mod m
    finger[next] = find_successor(n + 2^(next-1))

def check_predecessor():
    if predecessor has failed:
        predecessor = nil
```

`stabilize` is the load-bearing routine. It repairs the successor pointer whenever a node has inserted itself between `n` and `n`'s previous successor, and `notify` lets the newcomer register as predecessor. Executed on every node on a periodic timer, successor pointers converge while joins and departures proceed concurrently, **without global coordination**. `fix_fingers` performs the same repair for one finger entry per round; a stale finger costs additional hops, not a wrong answer.

Node failure is absorbed by the same structure. Each node keeps a **successor list of its next r successors** rather than a single pointer, and on failure of the immediate successor substitutes the next live entry. The paper shows that with **r = O(log N)** the ring survives simultaneous failure of half the nodes with high probability, since the probability that all r consecutive successors fail at once is small.

### Implementation sketch (Scala)

The load-bearing pieces are the half-open ring interval test and the descending finger scan. Both are easy to get wrong because the identifier space wraps.

```scala
type Id = Long                              // m-bit identifier, 0 until 1L << m

final case class Node(id: Id, finger: Vector[Id], successor: Id)

/** Membership in the ring interval (from, to), wrapping at 2^m. */
def inOpen(x: Id, from: Id, to: Id): Boolean =
  if from < to then from < x && x < to
  else from < x || x < to                   // interval crosses the origin

def inHalfOpen(x: Id, from: Id, to: Id): Boolean =
  inOpen(x, from, to) || x == to

/** Farthest finger that still precedes id; falls back to n itself. */
def closestPreceding(n: Node, id: Id): Id =
  n.finger.reverseIterator
    .find(f => inOpen(f, n.id, id))
    .getOrElse(n.id)

/** One routing step. Right means the answer, Left means forward to that node. */
def step(n: Node, id: Id): Either[Id, Id] =
  if inHalfOpen(id, n.id, n.successor) then Right(n.successor)
  else
    val next = closestPreceding(n, id)
    if next == n.id then Right(n.successor) // no useful finger: use ground truth
    else Left(next)
```

`step` returning `Right(n.successor)` when no finger helps is what keeps a fully stale finger table correct rather than divergent.

## Where this sits

Chord is the routing layer; consistent hashing is the placement rule it routes toward. Dynamo-style systems adopt the ring for placement without Chord's O(log N) routing, because every node holds a full membership list — practical at cluster scale, not at Internet scale. Chord applies where the membership list itself is too large or too volatile to replicate everywhere, solving the structured-overlay naming problem with logarithmic state instead of a full directory or a linear walk.

## Pitfalls

- **Interval tests written as closed rather than half-open.** A lookup for an identifier equal to the successor's own identifier is forwarded past its owner and loops, because `id ∈ (n, successor]` was coded as `id ∈ (n, successor)`.
- **Wrapping ignored in the comparison.** Ranges that cross identifier 0 test false under a naive `from < x && x < to`, so nodes near the origin are never selected as fingers and lookups degrade toward the O(N) successor walk.
- **Scanning the finger table forwards.** Taking the first matching entry instead of the farthest one yields a hop of minimum rather than maximum length; correctness holds, hop count rises toward O(N).
- **Treating fingers as ground truth.** Answering from a finger entry without the `id ∈ (n, successor]` test returns a wrong owner whenever that finger is stale, which converts a transient routing error into a data-correctness error.
- **Lookups issued during the convergence window.** Immediately after a join, before `stabilize` has repaired the affected successor pointers, a query can be answered by a node that no longer owns the key.
- **Successor list of length one.** With r = 1 the failure of a single successor breaks the ring at that point until stabilization repairs it; the paper's survival argument requires r = O(log N).
- **Stabilization period longer than the churn interval.** If nodes join and fail faster than `stabilize` runs, successor pointers never converge and the ring can fragment into disjoint loops.
