---
title: "Kademlia: the XOR Metric and Why Every Lookup Also Fixes the Routing Table"
date: 2026-08-27
track: distributed-systems
summary: "Chord routes over an asymmetric ring, so queries a node receives teach it nothing about the ring. Kademlia's XOR distance is symmetric: every incoming request is admissible routing state, k-buckets keep the longest-lived contacts, and the lookup is iterative and parallel — trading Chord's explicit stabilization protocol for maintenance that mostly rides on ordinary traffic."
reading_time: 7
tags: [kademlia, dht, xor-metric, k-buckets, p2p, churn, distributed-systems]
sources:
  - title: "Maymounkov, Mazières — Kademlia: A Peer-to-peer Information System Based on the XOR Metric (IPTPS 2002)"
    url: "https://pdos.csail.mit.edu/~petar/papers/maymounkov-kademlia-lncs.pdf"
  - title: "Stoica, Morris, Karger, Kaashoek, Balakrishnan — Chord: A Scalable Peer-to-peer Lookup Service for Internet Applications (SIGCOMM 2001)"
    url: "https://pdos.csail.mit.edu/papers/chord:sigcomm01/chord_sigcomm.pdf"
  - title: "BEP 5 — DHT Protocol (BitTorrent Mainline DHT)"
    url: "https://www.bittorrent.org/beps/bep_0005.html"
---

**Gist.** [Chord](/distributed-systems/2026-07-26-chord-dht-lookup) resolves a key in O(log N) hops but pays for it with a dedicated stabilization protocol, because its clockwise ring distance is asymmetric: a query arriving at a Chord node tells that node nothing usable about its own finger table. Kademlia (Maymounkov and Mazières, IPTPS 2002) defines distance as the bitwise exclusive or (XOR) of two identifiers, a metric that is **symmetric** — d(x, y) = d(y, x) — so every message a node receives arrives from a sender that is a legitimate candidate for the recipient's own routing table. Routing state is therefore refreshed as a **side effect of ordinary traffic**, and per-bucket contacts are ranked by observed lifetime, which trace data shows predicts future uptime. The cost is that the O(log N) hop bound is probabilistic rather than structural, lookups send redundant parallel requests, and buckets that see no traffic still need explicit hourly refreshes.

## The XOR metric

Kademlia assigns nodes and keys opaque 160-bit identifiers and defines the distance between two identifiers x and y as **d(x, y) = x ⊕ y, interpreted as an unsigned integer**. This is a valid (non-Euclidean) metric: d(x, x) = 0; d(x, y) > 0 for x ≠ y; and the triangle inequality d(x, y) + d(y, z) ≥ d(x, z) follows from a + b ≥ a ⊕ b for non-negative a, b. A key is stored on the k nodes whose identifiers are closest to it under this metric.

Two properties of ⊕ carry the whole design.

**Unidirectionality.** For any point x and distance Δ > 0 there is exactly one y with d(x, y) = Δ. Chord's clockwise metric has this property too, and it means all lookups for the same key converge along the same path regardless of where they start — which is what makes caching key/value pairs along the lookup path effective against hot spots.

**Symmetry.** d(x, y) = d(y, x), which Chord's metric lacks: node b can be one hop clockwise from a while a is nearly a full ring from b. The paper draws the consequence directly: under XOR, a node receives lookup queries **from precisely the same distribution of nodes contained in its own routing table**, so every incoming request carries a contact the recipient can use. Chord does not learn useful routing information from the queries it receives. Asymmetry also makes Chord's tables rigid — each finger entry must be the precise node succeeding some interval — whereas a Kademlia bucket entry may be any node in the interval, leaving room to pick contacts by latency or to query several in parallel.

Geometrically, XOR distance is prefix distance: nodes are leaves of a binary tree positioned by their identifier bits, and the distance between two identifiers is determined by the height of the smallest subtree containing both. Halving the XOR distance means extending the shared prefix by one bit.

## k-buckets: longevity as an eviction policy

For each 0 ≤ i < 160, a node keeps a list — a **k-bucket** — of contacts (IP address, UDP port, node ID) at distance between 2^i and 2^(i+1) from itself. Each bucket holds at most k entries, sorted least-recently seen at the head; the paper suggests **k = 20**, chosen so that k nodes are unlikely to all fail within an hour. BitTorrent's Mainline DHT (BEP 5) ships the same structure with k = 8.

When any message arrives, the sender's contact is inserted into the appropriate bucket:

- Sender already present → move it to the tail (most recently seen).
- Bucket has fewer than k entries → append the sender at the tail.
- Bucket full → **ping the least-recently seen entry**. If it fails to respond, evict it and append the sender; if it responds, keep it and **discard the new contact**.

That last rule is the load-bearing one: **a live node is never evicted from a k-bucket**. The justification is empirical. The paper's analysis of Gnutella trace data (collected by Saroiu et al.) shows the probability that a node stays online another hour *increases* with how long it has already been up. Preferring the oldest live contacts therefore maximizes the probability that bucket entries remain reachable — the property that makes the routing table churn-resistant without any repair protocol. The same rule yields resistance to a class of denial-of-service attack: flooding the network with new node identities cannot flush established routing state, because newcomers are only admitted when old contacts die.

The routing table itself is a binary tree of buckets grown lazily: a node starts with one bucket covering the whole space and **splits a full bucket only when its range contains the node's own ID** (with a relaxation for unbalanced trees so the node still knows at least k contacts in its surrounding subtree). The result is fine-grained knowledge of the nearby identifier space and coarse knowledge of the far side — the same doubling structure as Chord's fingers, but with k candidates per interval instead of one mandated successor.

## Iterative parallel lookup

Kademlia uses four remote procedure calls (RPCs): PING, STORE, FIND_NODE, and FIND_VALUE. FIND_NODE takes an ID and returns the k closest contacts the recipient knows. Everything else is built on the *node lookup*: locating the k closest nodes to a target ID.

The lookup is **iterative** — the initiator drives every step, unlike Chord's formulation where the query can be forwarded node to node — and **parallel**:

1. Pick the **α** closest contacts to the target from the local table (α is a system-wide concurrency parameter; the paper suggests α = 3) and send them asynchronous FIND_NODE RPCs.
2. As replies arrive, merge the returned contacts into a candidate set ordered by XOR distance, and re-issue FIND_NODE to α of the closest not-yet-queried candidates. The recursion may begin before all outstanding RPCs return; nodes that fail to respond quickly are set aside unless they answer later.
3. If a round produces no candidate closer than the closest already seen, query **all** of the k closest unqueried candidates.
4. Terminate when the initiator has queried and received responses from the k closest nodes it has seen.

Because each useful hop extends the matched prefix, the remaining distance at least halves per step and the lookup contacts O(log N) nodes; the paper's proof sketch bounds the procedure at ⌈log n⌉ + c steps for a small constant c, *assuming the invariant that every bucket with an eligible node holds at least one contact*. With α = 1 the message cost resembles Chord's; the point of α > 1 is **latency under failure**: a dead or slow node stalls one of α concurrent probes rather than the whole lookup, so timeouts are absorbed instead of serialized. The redundancy is the price — α − 1 of each round's RPCs are, in the best case, wasted messages.

FIND_VALUE is FIND_NODE with a short-circuit: any node holding the key returns the value and the lookup halts. The requester then caches the pair at the closest observed node that did *not* hold it, so subsequent lookups — which, by unidirectionality, follow converging paths — hit the cache earlier.

## What replaces stabilization

Chord's correctness under churn rests on `stabilize`, `notify`, `fix_fingers`, and `check_predecessor` running periodically on every node forever, whether or not anyone performs a lookup. Kademlia's table maintenance is mostly **traffic-driven**: every request and every reply updates a bucket, so buckets on well-trafficked paths stay fresh for free. The explicit residue is small: a node refreshes any bucket in which it has performed no lookup for **an hour** by searching for a random ID in that bucket's range (BEP 5 tightens this to 15 minutes), and stored pairs are republished hourly to survive node departures. Joining is one node lookup for the joiner's own ID plus refreshes of the buckets beyond its nearest neighbor.

The trade is therefore: Chord buys a structurally guaranteed O(log N) hop count with continuous background repair and rigid table entries; Kademlia buys cheap, attack-resistant, traffic-maintained tables with a hop bound that holds **with high probability** under random identifiers and the bucket-occupancy invariant, plus α-fold message redundancy per lookup. In deployment the trade has gone one way: BitTorrent's Mainline DHT, running Kademlia per BEP 5, is the largest DHT in operation.

### Implementation sketch (Scala)

The mechanism worth making precise is the bucket update rule, since it encodes the entire churn argument.

```scala
type Id = BigInt                       // 160-bit identifier

def distance(a: Id, b: Id): Id = a ^ b // XOR, compared as an unsigned integer

/** Bucket index: position of the highest differing bit. */
def bucketIndex(self: Id, other: Id): Int =
  distance(self, other).bitLength - 1

final case class Contact(id: Id /* address elided */)

/** Head = least-recently seen, tail = most-recently seen. */
final case class KBucket(entries: Vector[Contact], k: Int) {

  /** Called for the sender of EVERY received message. */
  def seen(c: Contact, pingHead: Contact => Boolean): KBucket =
    if (entries.exists(_.id == c.id))
      copy(entries = entries.filterNot(_.id == c.id) :+ c)   // move to tail
    else if (entries.size < k)
      copy(entries = entries :+ c)                           // room: append
    else if (pingHead(entries.head))
      copy(entries = entries.tail :+ entries.head)           // head alive:
                                                             // keep it, DROP c
    else
      copy(entries = entries.tail :+ c)                      // head dead: evict
}
```

`seen` is invoked from the RPC layer for requests and replies alike — that call site is where the symmetry of XOR becomes an operational property rather than an algebraic one.

## Pitfalls

- **Treating the sender's ID as verified.** Bucket entries come from self-reported IDs in received messages; without the random RPC ID echo (or stronger authentication), an attacker can pollute tables by forging sources.
- **Evicting live contacts to admit newcomers.** Replacing a responsive head with the new sender inverts the uptime heuristic, fills tables with short-lived nodes, and reopens the flooding attack the discard rule exists to block.
- **Terminating the lookup at the first non-improving round.** The protocol requires a final round querying all k closest unqueried candidates; stopping early returns a set that is close but not the k closest, which silently misplaces STOREs.
- **Removing slow nodes from the candidate set permanently.** The paper removes unresponsive nodes from consideration *until and unless* they respond; discarding them outright turns transient congestion into lost coverage.
- **Splitting every full bucket.** Buckets are split only when their range covers the node's own ID (plus the unbalanced-tree relaxation); splitting everywhere reproduces O(N) state and forfeits the log-scale table.
- **Comparing XOR distances as signed integers.** With 160-bit IDs packed into fixed-width machine words, a signed comparison misorders distances whose high bit differs, and the lookup walks away from the target.
- **Skipping idle-bucket refreshes.** Traffic keeps only trafficked buckets fresh; a bucket with no lookups for an hour holds progressively staler contacts, and the occupancy invariant behind the ⌈log n⌉ + c bound erodes.
