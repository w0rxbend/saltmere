---
title: "Shuffle sharding: fault isolation that plain sharding cannot buy"
date: 2026-07-31
track: sys-patterns
summary: "Plain sharding confines a bad tenant to one shard, but every co-tenant of that shard fails with it. Shuffle sharding assigns each tenant a random combination of nodes, so full overlap between two tenants becomes combinatorially rare. The cost is a retrying client and reduced containment as shard size grows."
reading_time: 6
tags: [sharding, fault-isolation, blast-radius, multi-tenancy, resilience]
sources:
  - title: "Workload isolation using shuffle-sharding — Amazon Builders' Library (Colm MacCárthaigh)"
    url: "https://aws.amazon.com/builders-library/workload-isolation-using-shuffle-sharding/"
  - title: "Shuffle Sharding: massive and magical fault isolation — AWS Architecture Blog"
    url: "https://aws.amazon.com/blogs/architecture/shuffle-sharding-massive-and-magical-fault-isolation"
  - title: "Shuffle sharding in Cortex/Mimir (worked example of the technique)"
    url: "https://cortexmetrics.io/docs/guides/shuffle-sharding/"
---

**Gist.** In a multi-tenant fleet, one tenant issuing a poison workload — an expensive query, a retry storm, accidental denial of service — degrades whatever nodes it can reach, and plain sharding converts that into a total outage for every tenant sharing its shard. Shuffle sharding instead assigns each tenant a **random combination** of nodes drawn from the whole fleet, so two tenants share their *entire* shard only with probability on the order of 1 / C(n, k) for a fleet of n nodes and shard size k. The mechanism only pays off if the client retries other nodes in its own shard, and containment weakens as k grows.

## The blast radius of the alternatives

Consider a fleet of 8 worker nodes behind a router.

**No sharding.** The poison tenant reaches any node, so all 8 eventually degrade. Every tenant is affected, each partially.

**Plain sharding.** Each tenant is assigned to one fixed shard — say 2 of the 8 nodes. Damage is contained to that shard, but every other tenant assigned to the same shard shares the poison tenant's fate completely. The trade is "everyone a little" for "a fixed group entirely". For that group the result is an outage, not a degradation.

The failure mode plain sharding cannot avoid is that **shard membership is an equivalence relation**: tenants are partitioned into disjoint groups, and a group either survives together or fails together. There is no partial overlap to exploit.

## The shuffle

Shuffle sharding, described in the Amazon Builders' Library, changes how the shard is chosen. Rather than selecting one of a few fixed shards, each tenant receives an arbitrary k-subset of the n nodes. With n = 8 and k = 2 there are C(8, 2) = **28** distinct pairs rather than 4 disjoint ones.

The invariant that matters is not "no two tenants share a node" — with a small fleet that is unachievable. It is weaker and more useful: **a tenant is fully unavailable only when every node in its shard is also in the poison tenant's shard**, that is, when its shard is a subset of the damaged set. Partial overlap leaves at least one healthy node, and a client that retries within its own shard recovers on that node.

The probability of full overlap for a second tenant drawn uniformly at random is roughly **C(k, k) / C(n, k) = 1 / C(n, k)**. At n = 100 and k = 5 there are C(100, 5) ≈ **75 million** combinations, so the odds that all 5 of one tenant's nodes fall inside another's 5 are about 1 in 75 million. Enlarging the fleet at fixed k inflates the denominator; enlarging k does not do so monotonically, since C(n, k) falls again once k passes n / 2 and reaches 1 at k = n.

The consequence is asymmetric and is the point of the pattern: a single bad tenant makes a node hot for a modest number of other tenants, while **almost no tenant loses its whole shard**, so almost every tenant retains a healthy node to retry against.

## Assigning shards deterministically

No assignment table is stored. Each tenant's shard is **derived from its identifier**, so every router computes the same set without coordination. The derivation must sample **without replacement**, otherwise a shard of nominal size k can contain fewer than k distinct nodes and the redundancy the pattern depends on silently shrinks.

### Implementation sketch (Scala)

```scala
import java.security.MessageDigest

/** Derives a tenant's shard: `size` distinct nodes, chosen deterministically
  * from `nodes`, with no stored assignment table. */
def shardFor(tenantId: String, nodes: IndexedSeq[String], size: Int): IndexedSeq[String] =
  val digest = MessageDigest.getInstance("SHA-256")

  def index(i: Int, poolSize: Int): Int =
    val h = digest.digest(s"$tenantId:$i".getBytes("UTF-8"))
    // First 8 digest bytes as a Long; the shift drops the sign bit so `%` stays non-negative.
    val v = h.take(8).foldLeft(0L)((acc, b) => (acc << 8) | (b & 0xffL)) >>> 1
    (v % poolSize).toInt

  val (chosen, _) =
    (0 until size).foldLeft((Vector.empty[String], nodes)):
      case ((acc, pool), i) =>
        val j = index(i, pool.size)
        // Removal is what makes this sampling WITHOUT replacement.
        (acc :+ pool(j), pool.patch(j, Nil, 1))

  chosen

/** Full overlap — the only case in which the tenant has nothing left to retry. */
def fullyEclipsed(victim: Set[String], damaged: Set[String]): Boolean =
  victim.subsetOf(damaged)
```

The routing rule is then: send a tenant's traffic only to `shardFor(tenantId, …)`, and **have the client retry a different node of that same shard on failure**. The retry is not an optimisation but the second half of the pattern — overlap analysis produces partial redundancy, and the retrying client is what converts that redundancy into availability. Without it, one damaged node in a shard is as bad as all of them.

## Limits of the technique

Shuffle sharding isolates **independent** failures: a poison tenant, a bad host. It does not mitigate a defect that crashes every node identically, because that is a fleet-wide correlated failure and no shard geometry contains it.

Shard size is a genuine dial with opposing effects. **Larger k gives each tenant more redundancy but increases pairwise overlap between tenants**, so containment degrades as k approaches n; at k = n the scheme reduces to no sharding at all. Smaller k improves isolation but leaves each tenant fewer nodes to retry against and concentrates its capacity.

The technique is used in production by AWS Route 53 and by Cortex/Mimir, whose documentation gives a worked configuration of the same construction.

A simulation makes the effect concrete: draw shards for 10,000 tenants over 100 nodes at shard size 5, mark one tenant's nodes as damaged, then count the tenants that lost *all* of their nodes against those that lost *at least one*. The gap between those two counts is the availability that shuffle sharding purchases, and it is entirely dependent on the client retrying.

## Pitfalls

- **Sampling with replacement instead of without.** A shard nominally of size k contains duplicate nodes, so the effective redundancy is lower than configured and full-overlap events occur far more often than 1 / C(n, k) predicts.
- **No client-side retry within the shard.** Every tenant with even one damaged node in its shard sees errors, which removes the entire benefit; partial overlap only helps if something retries.
- **Treating shuffle sharding as protection against correlated failure.** A deployment of poisoned code or a shared dependency outage takes down all shards simultaneously; the observed blast radius is the whole fleet regardless of shard geometry.
- **Raising shard size to improve availability.** Larger shards increase per-tenant redundancy but also pairwise overlap, so isolation falls; at the extreme k = n every tenant sees every node and containment is gone.
- **Recomputing shards on every membership change.** Because the shard is derived from the node list, adding or removing a node reshuffles assignments for tenants that were not otherwise affected, moving traffic and warm state unexpectedly.
- **Assuming uniform tenant load.** The 1 / C(n, k) estimate treats tenants as uniformly drawn; a few very large tenants concentrated on overlapping nodes make the realised distribution worse than the combinatorial bound suggests.
