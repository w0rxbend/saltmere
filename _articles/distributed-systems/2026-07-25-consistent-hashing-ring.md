---
title: "The consistent hash ring: move a node, remap only K/N keys"
date: 2026-07-25
track: distributed-systems
summary: "Modulo hashing remaps almost every key when the cluster resizes. A hash ring with virtual nodes remaps only about K/N of them. The ring, the argument for the bound, and a compact implementation."
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

**Gist.** A partitioning scheme must map `K` keys onto `N` nodes and keep that mapping stable as `N` changes; the textbook `node = hash(key) % N` fails because changing the divisor changes the residue of nearly every key, so a 10-node cluster gaining an eleventh node relocates roughly 90% of its keys at once. The hash ring of Karger et al. (1997) hashes keys and nodes into one circular identifier space and assigns each key to the first node clockwise from it, so a membership change disturbs only that node's arc — on average `K/N` keys. The cost is a second layer of bookkeeping: each physical node must be replicated into many **virtual nodes** to make the arcs even, and every client needs a consistent view of the full ring rather than a single integer `N`.

## The ring and its invariant

Both keys and nodes are hashed into the same identifier space — for example the 128-bit output of a hash function, treated as a circle by wrapping at `2^128`. Ownership follows one rule: **a key belongs to the first node position encountered walking clockwise from the key's position**. Nothing else is consulted; the mapping is a pure function of the key's hash and the set of node positions currently on the ring. This is the same structured-naming construction van Steen & Tanenbaum use to locate entities in a distributed hash table (DHT): hash the name into a circular identifier space and let position decide ownership.

The bound follows from that rule. Removing a node deletes one position; the only keys whose clockwise successor changes are those lying in the arc that terminated at the deleted position, and they slide to the next surviving node. Every other key's successor is untouched, so its owner is unchanged. Adding a node is the mirror image: the new position captures exactly the arc between itself and its counter-clockwise neighbour, taking those keys from that neighbour's successor and from nowhere else. **Keys move only to or from the node being added or removed; they never churn between two nodes that both stayed in the cluster.** Karger et al. call this property monotonicity. The number of keys affected is the number in one node's arc, which under a uniform hash is `K/N` in expectation.

Modulo hashing has no such structure. `hash(key) % N` and `hash(key) % (N+1)` agree only when the two residues coincide by chance, which happens for about `1/(N+1)` of keys — so about `N/(N+1)` of the key space is remapped, and the movement is not confined to the arriving node.

## Virtual nodes

With one ring position per physical node, the arc lengths are the gaps between `N` random points on a circle, so they are uneven: some node receives a noticeably larger share and becomes a hotspot. The second problem is failure behaviour — a node with a single arc hands its entire share to one successor, doubling that successor's load at the worst possible moment.

The remedy is to hash each physical node to `V` distinct positions, derived from distinct labels (`node#0`, `node#1`, and so on), and record a position-to-physical-node mapping. **Averaging `V` independent arcs per physical node narrows the spread of the per-node share**, and Karger et al. show that giving each node on the order of `log N` virtual copies makes the arcs balanced with high probability. Failure behaviour improves for a separate reason: the departing node's `V` small arcs have `V` different clockwise successors, so **the lost share is spread across many nodes rather than dumped on one**. Dynamo partitions its key space with this ring-plus-virtual-node construction; van Steen & Tanenbaum present the same technique as balancing the key space across a structured overlay.

### Implementation sketch (Scala)

The clockwise walk is a binary search over a sorted array of ring positions. The load-bearing detail is the wrap: a key hashing past the last position belongs to the first.

```scala
final class HashRing(nodes: Set[String], vnodes: Int = 150):
  // Ring positions in ascending order, plus position -> physical node.
  private val ring: Vector[(Long, String)] =
    nodes.toVector
      .flatMap(n => (0 until vnodes).map(i => hash(s"$n#$i") -> n))
      .sortBy(_._1)

  private val positions: Array[Long] = ring.map(_._1).toArray

  private def hash(s: String): Long =
    val d = java.security.MessageDigest.getInstance("MD5").digest(s.getBytes("UTF-8"))
    // Top 64 bits as a signed Long; sorting and searching use the same ordering.
    (0 until 8).foldLeft(0L)((acc, i) => (acc << 8) | (d(i) & 0xffL))

  def get(key: String): Option[String] =
    if ring.isEmpty then None
    else
      val h = hash(key)
      val i = java.util.Arrays.binarySearch(positions, h) match
        case found if found >= 0 => found          // exact hit on a position
        case ins                 => -(ins + 1)     // first position greater than h
      Some(ring(i % ring.size)._2)                 // modulo closes the circle

  def added(node: String): HashRing = HashRing(nodes + node, vnodes)
  def removed(node: String): HashRing = HashRing(nodes - node, vnodes)
```

`java.util.Arrays.binarySearch` returns `-(insertionPoint) - 1` when the key is absent, which is why the miss branch negates and decrements; the insertion point is precisely the index of the first ring position greater than `h`, that is, the clockwise successor. When `h` exceeds every position the insertion point equals `ring.size`, and the modulo maps it back to index 0.

## Verifying the bound empirically

The claim is checkable without a cluster: assign a large key set over `N` nodes, snapshot the mapping, add one node, and count the keys whose owner changed. For `N = 10` the ring is expected to move about `1/(N+1)` of the keys — roughly 9%. The same experiment against `hash(key) % N` should move about `N/(N+1)`, roughly 90%. That difference is why DHTs, sharded caches, and the Dynamo-lineage partitioners are built on rings rather than on a divisor.

Extending `get` to return the next `R` **distinct** physical nodes clockwise, rather than one, yields the preference list onto which Dynamo replicates each key, connecting the ring to quorum parameters `(N, R, W)`.

## Pitfalls

- **Deduplicating by ring position instead of by physical node when building a replica list.** Walking `R` positions clockwise can return several virtual nodes belonging to the same machine, so a replica set of size `R` lands on fewer than `R` distinct hosts and the intended redundancy does not exist.
- **Two clients disagreeing about `vnodes` or the label format.** The positions are derived from `s"$node#$i"`; a client using a different separator, count, or hash function computes a different ring and routes the same key elsewhere, producing reads that miss data written by the other client.
- **Reusing a node's identifier for a replacement machine.** The new machine occupies the retired machine's ring positions and immediately owns its arcs, so requests are routed to a host that does not hold the data yet.
- **Assuming even keys imply even bytes.** The ring balances key counts, not key sizes or request rates; a single large or hot key sits in one arc and one node absorbs it regardless of how many virtual nodes are configured.
- **Non-uniform key hashing.** If the identifier space is entered through a hash with structure — a truncated identifier or a low-entropy prefix — keys cluster on a few arcs and the `K/N` expectation, which assumes uniformity, no longer describes the observed load.
