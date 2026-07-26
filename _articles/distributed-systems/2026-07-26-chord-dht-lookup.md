---
title: "Chord: finding any key in O(log N) hops, no directory required"
date: 2026-07-26
track: distributed-systems
summary: "The consistent hash ring tells you who owns a key. Chord tells you how to find that owner without every node knowing every other node: finger tables, the find_successor lookup, and the stabilization protocol that keeps it all correct while nodes join and leave."
reading_time: 5
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

The [consistent hash ring](/distributed-systems/2026-07-25-consistent-hashing-ring) answers "who owns this key" with a rule: walk clockwise from `hash(key)` to the first node. That rule is easy to state and cheap to prove correct — but it silently assumes every node already knows the position of every other node, so it can just compute the answer locally. In a peer-to-peer network with thousands of nodes churning constantly, nobody has that global picture. Chord (Stoica, Morris, Karger, Kaashoek, Balakrishnan, SIGCOMM 2001) is the protocol for finding a key's owner when each node only knows a handful of others — in O(log N) messages, with correctness maintained automatically as nodes join and leave.

## The ring, minimally

Chord hashes both nodes and keys into the same m-bit identifier space (SHA-1, so typically m = 160), arranged as a circle of size 2^m — this part is exactly the consistent-hashing idea. A key `k` is owned by its **successor**: the first node whose identifier is ≥ `k` going clockwise. Every node stores two pointers into this ring: `successor` (next node clockwise) and `predecessor` (previous node counter-clockwise).

With only successor pointers, a lookup is a linear walk around the ring — correct, but O(N) hops. Chord's contribution is a routing table, the **finger table**, that turns that walk into a binary search.

## Finger tables

Node `n`'s finger table has up to m entries. The i-th entry points to the first node that succeeds `n + 2^(i-1)` (mod 2^m):

```
finger[i].start  = (n + 2^(i-1)) mod 2^m        for i = 1 .. m
finger[i].node   = successor(finger[i].start)
```

So `finger[1]` is just `successor`. `finger[2]` points roughly a quarter of the way around from `finger[1]`'s target, `finger[3]` an eighth further, and so on — each entry doubles the reach of the previous one. A node with m = 160 stores at most 160 pointers no matter how large the network gets: O(log N) state per node for an N-node network, not O(N).

| | flat successor chain | Chord finger table |
|---|---|---|
| State per node | O(1) (successor only) | O(log N) |
| Lookup hops | O(N) | O(log N) expected |
| Join cost (fingers to fix) | — | O(log² N) messages |
| Correctness under churn | breaks if successor is stale | self-heals via stabilization |

## The lookup: find_successor

To resolve key `id`, a node checks whether the key falls between itself and its immediate successor. If not, it doesn't send the query to its successor — it jumps to the finger table entry that gets it *closest to the target without overshooting*, then that node repeats the process:

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

Each hop at least halves the remaining distance to the target on the ring, because `closest_preceding_node` picks the farthest finger that doesn't pass `id`. That's the whole argument for O(log N) expected hops, and it's the same halving argument as binary search — just walking a circle instead of an array. The original paper's simulations bear this out: for a 64,000-node network the mean lookup path length is about 0.5 · log₂(N) hops, and an early PlanetLab-style deployment across ten Internet sites measured real lookup latencies of roughly 180-285 ms.

## Join and leave: three invariants to protect

A joining node `n` only needs one contact, some existing node `n'`, to bootstrap:

```
// n.join(n')
predecessor = nil
successor   = n'.find_successor(n)
```

That single `find_successor` call correctly places `n` in the ring. But three invariants have to stay true for lookups to keep working: (1) every node's successor pointer is correct, (2) every key is stored at its true successor, and (3) finger tables are reasonably fresh. Fixing all three synchronously on every join/leave would be expensive and fragile under concurrent changes — real networks have nodes joining, leaving, and failing simultaneously. Chord's answer is to relax invariant (3) and let a background process fix it up continuously.

## Stabilization: eventually-correct successors

Each node periodically runs three routines instead of trying to update the world atomically at join time:

```
// runs periodically at node n
def stabilize():
    x = successor.predecessor
    if x ∈ (n, successor):
        successor = x                 # a closer successor showed up
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

`stabilize` is the load-bearing routine: it repairs the successor pointer whenever a new node has inserted itself between `n` and `n`'s old successor, and `notify` lets that new node register itself as the predecessor. Run this on every node every few seconds and successor pointers — and therefore correctness of lookups — converge even while nodes are joining and leaving concurrently, without any global coordination. `fix_fingers` does the same job for the finger table, just lazily; a slightly stale finger only costs an extra hop or two, never a wrong answer, because `find_successor` always falls back to the successor pointer as ground truth.

Node failure is handled the same way successors are: each node keeps a **successor list** of its next r successors (not just one). If the immediate successor dies, the node substitutes the next live entry from that list. The paper proves that with r = O(log N), the ring survives even simultaneous failure of half the nodes with high probability, because it's vanishingly unlikely that r consecutive successors all fail at once.

## Where this sits

Chord is the routing layer; consistent hashing is the placement rule it routes toward. Dynamo-style systems use the ring for placement but skip Chord's O(log N) routing because every node keeps a full membership list (feasible at cluster scale, not at Internet scale). Chord is what you reach for when the membership list itself is too large or too volatile to replicate everywhere — the same naming problem van Steen & Tanenbaum pose for structured overlays, solved with logarithmic state instead of either a full directory or a linear walk.

**Try next:** implement `find_successor` and `stabilize` for a ring of ~16 simulated nodes with m = 8, kill a random node every few stabilization rounds, and plot how many lookups return a wrong answer during convergence versus how many hops a correct lookup takes as N grows.
