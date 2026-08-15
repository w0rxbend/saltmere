---
title: 'Rendezvous hashing (HRW): pick the highest score, skip the ring'
date: 2026-08-10
track: distributed-systems
summary: 'Consistent hashing needs a ring and hundreds of virtual nodes per host to stay balanced. Rendezvous hashing reaches the same 1/N remapping bound and the same even load with no bookkeeping: for each key, score every node and take the maximum. Why it works, an implementation, and how it compares with the ring and with jump hash.'
reading_time: 7
tags:
- rendezvous-hashing
- hrw
- consistent-hashing
- sharding
- load-balancing
sources:
- title: Thaler & Ravishankar, Using Name-Based Mappings to Increase Hit Rates (IEEE/ACM ToN, Feb 1998)
  url: https://www.microsoft.com/en-us/research/wp-content/uploads/2017/02/HRW98.pdf
- title: Rendezvous hashing — Wikipedia
  url: https://en.wikipedia.org/wiki/Rendezvous_hashing
- title: Envoy — Supported load balancers (ring hash vs Maglev)
  url: https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/load_balancing/load_balancers.html
- title: Lamping & Veach, A Fast, Minimal Memory, Consistent Hash Algorithm (jump hash)
  url: https://arxiv.org/abs/1406.2294
- title: 'Damian Gryski, Consistent Hashing: Algorithmic Tradeoffs'
  url: https://dgryski.medium.com/consistent-hashing-algorithmic-tradeoffs-ef6b8e2fcae8
- title: Thaler & Ravishankar — Using Name-Based Mappings to Increase Hit Rates (IEEE/ACM Trans. Networking, Feb 1998)
  url: https://www.semanticscholar.org/paper/Using-name-based-mappings-to-increase-hit-rates-Thaler-Ravishankar/6a3d10bb30818c86c18cef1e5e4b128ae80840ae
- title: 'Consistent Hashing vs. Rendezvous Hashing: A Comparison (DZone)'
  url: https://dzone.com/articles/consistent-hashing-vs-rendezvous-hashing-a-compara
---

**Gist.** Mapping `K` keys onto a changing set of `N` servers requires a rule that is deterministic and coordination-free, evenly balanced, and stable under membership change; `hash(key) % N` satisfies the first two and relocates nearly every key when `N` changes. Rendezvous hashing — **highest random weight (HRW)** hashing, from Thaler & Ravishankar (IEEE/ACM Transactions on Networking, February 1998) — assigns each key to the node maximising a hash of the pair `(key, node)`, which yields `1/N` load per node and moves only the keys owned by a departing node, with **no ring, no virtual nodes, and no persistent structure**. The cost is **O(N) hashes per lookup** rather than the ring's O(log N) binary search.

## The mechanism: score every node, take the maximum

Placement is a single expression. For a key, compute a hash of `(key, node)` for *every* node in the current set and assign the key to the node with the highest score. That node is where all clients "rendezvous" for the key, which names the scheme.

```scala
def owner(key: String, nodes: Seq[String]): String =
  nodes.maxBy(node => scala.util.hashing.MurmurHash3.stringHash(s"$node:$key"))

owner("user:42", Seq("cache-0", "cache-1", "cache-2", "cache-3"))
```

There is no `add()`, no `remove()`, and no persistent data structure. **The node list is the entire state**, and a membership change is a different argument to `max`. Two clients agree on placement precisely when they hold the same node list; disagreement over the list, not over the algorithm, is the only source of divergence.

## Balance and the remapping bound

**Even load.** The hash mixes key and node together, so for a fixed key the scores of the nodes behave as independent uniform draws. Each node is equally likely to hold the maximum, so each owns approximately `1/N` of the keyspace, with no virtual nodes. The ring requires virtual nodes for the opposite reason: a single hash point per node partitions the circle into arcs of unequal length, and only averaging over many points per node smooths them. HRW's per-key contest is uniform by construction, so there is nothing to tune.

**Minimal remapping.** Consider removing a node. A key is affected only if the removed node held its *maximum* score. If it did, the key falls to the node with the *second*-highest score, which is a fresh independent draw over the survivors. If it did not, the maximum is untouched and the key does not move. Exactly the removed node's keys relocate — on average `1/N` of the keyspace — and no other key changes owner. Addition is the mirror image: a new node wins a key only when its score beats the incumbent maximum, which happens for approximately `1/(N+1)` of keys, all of them moving *toward* the new node and never between existing nodes. **Keys only ever move to or from the node whose membership changed**, which is the same monotonicity property the ring provides, obtained here without maintaining a structure.

## Cost: O(N) per lookup, and the documented mitigation

Every lookup hashes the key against *all* `N` nodes: **O(N) per key**, against O(log N) for a binary search on a sorted ring. For clusters of a few dozen to a few hundred nodes the per-node hash is inexpensive and there is no ring to rebuild when membership changes. HRW is therefore best matched to node sets that are **small and change often**.

For large `N`, Wikipedia documents **skeleton-based HRW**: nodes are placed as leaves of a virtual tree and HRW is applied level by level down the skeleton, scoring only a node's children at each level, giving **O(log N) lookups**. The documented trade-off is that the hierarchy weakens the global uniformity of placement in exchange for the speed-up.

A **weighted** variant handles heterogeneous capacities. The raw hash is mapped into `(0,1]` as `u`, and the score becomes `-w / ln(u)`, a logarithmic reshaping under which win probability is proportional to `w`. Changing a weight changes a multiplier; unlike ring virtual nodes, no points are added or removed.

## Comparison with the ring and with jump hash

Against the [consistent hash ring](/articles/distributed-systems/2026-07-25-consistent-hashing-ring): both reach the `1/N` remapping bound. The ring's balance depends on tuning `V` virtual nodes per host, with load error shrinking as `1/sqrt(V)`, at the cost of memory proportional to `N·V` and a rebuild on every membership change. HRW is balanced without tuning and holds no structure, but pays O(N) per lookup rather than O(log N). **Small, churning node sets favour HRW; large stable ones favour the ring.**

Against **jump consistent hash** (Lamping & Veach): jump hash runs in O(ln N) time with zero memory in a few lines and is well balanced. Its restriction is structural — it maps keys to bucket *numbers* `0..N-1` and supports removing only the *last* bucket. Removing a bucket from the middle is not expressible, and mapping to named or heterogeneous nodes requires an indirection layer. HRW handles arbitrary named nodes and arbitrary removals directly.

## Deployments

Thaler & Ravishankar's stated setting was **web cache arrays**: a proxy selecting which cache in an array holds an object, by name, so that clients agree without a coordinator. Wikipedia lists deployments including GitHub's load balancer, Apache Ignite, Apache Druid, Twitter's EventBus, IBM Cloud Object Storage, and Ceph's CRUSH, whose placement draws on rendezvous hashing.

**Envoy** ships two consistent-hash load balancers — *ring hash* (Ketama-style) and *Maglev* — and not rendezvous hashing. Its documentation records that Maglev's table construction and lookup are substantially faster than a large ring, but that Maglev is "not as stable as ring hash when upstream hosts change", with stability tunable through `table_size`. HRW occupies a different corner of that space: stable and balanced without tuning, at the cost of per-request O(N) scoring.

## Weighting and replica sets

The weighted score in closed form maps the raw hash into `(0,1]` as `u` and returns `-w / ln(u)`; the implementation sketch below computes it directly.

Replication follows from the same ordering: instead of the single argmax, take the **top-k nodes by score**. Those k form the replica set, and when one of them leaves, only its share of keys shifts to the next-highest node — the replica each key gains is exactly the (k+1)-th ranked node in the ordering it already computed. No shared ring has to be kept consistent across clients.

### Implementation sketch (Scala)

```scala
final case class Node(name: String, weight: Double = 1.0)

object Hrw:
  /** Two 32-bit hashes of the pair, in both orders, packed into 64 bits; the
    * colon separates the fields, which requires node names to exclude it. */
  private def raw(key: String, node: Node): Long =
    val h = scala.util.hashing.MurmurHash3.stringHash(s"${node.name}:$key")
    val g = scala.util.hashing.MurmurHash3.stringHash(s"$key:${node.name}")
    (h.toLong << 32) | (g.toLong & 0xffffffffL)

  /** Maps the raw hash into (0,1] and applies the -w / ln(u) reshaping, under
    * which win probability is proportional to weight. */
  private def score(key: String, node: Node): Double =
    val u = ((raw(key, node) >>> 11).toDouble + 1.0) / (1L << 53).toDouble
    node.weight / -math.log(u)

  def owner(key: String, nodes: Seq[Node]): Node =
    nodes.maxBy(score(key, _))          // O(N) scores, no persistent state

  /** The replica set is the top k of the same ordering; losing one member
    * promotes the (k+1)-th ranked node and touches no other key. */
  def replicas(key: String, nodes: Seq[Node], k: Int): Seq[Node] =
    nodes.sortBy(n => -score(key, n)).take(k)
```

## Pitfalls

- **Clients holding different node lists place the same key differently.** The algorithm is deterministic, but its only input is the membership list; a client that has not yet observed a removal keeps routing to the dead node while others have already promoted the runner-up.
- **Ties in the score are resolved by the surrounding code, not by HRW.** Scala's `maxBy` returns the first maximal element, so two nodes with equal scores are broken by list order; a client that iterates its node list in a different order will pick a different owner for that key.
- **Concatenating key and node without a separator collides.** Hashing `node + key` makes the pairs `("cache-1", "0:x")` and `("cache-10", ":x")` produce the same input string, so unrelated keys share a score.
- **O(N) scoring per lookup grows with the cluster.** A node set large enough for the per-lookup cost to matter is the case skeleton-based HRW addresses, at the documented cost of weaker global uniformity of placement.
- **Changing the hash function remaps everything.** The mapping is defined entirely by the scoring function, so replacing it is equivalent to a full reshard, not an incremental membership change.
- **Weights act through their ratios, not their absolute values.** Under the `-w / ln(u)` transform the share of a node is proportional to its weight, so scaling every weight by the same factor leaves placement unchanged; a migration that copies ring virtual-node counts into the weight field preserves the intended shares only because their ratios happen to match.
