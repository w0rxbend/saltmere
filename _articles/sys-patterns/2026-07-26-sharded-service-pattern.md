---
title: "The Sharded Service: When Replicas Cannot Hold All the State"
date: 2026-07-26
track: sys-patterns
summary: "Replicated services scale by cloning; sharded services scale by dividing. An examination of Burns' sharded-service pattern — the sharding function, hot shards, rebalancing on growth, and a sharded cache in front of a scatter-gather root."
reading_time: 7
tags: [sharding, hot-shards, caching, scalability, kubernetes, statefulset, burns]
sources:
  - title: "Designing Distributed Systems — Sharded Services (Burns, O'Reilly)"
    url: "https://www.oreilly.com/library/view/designing-distributed-systems/9781491983638/ch06.html"
  - title: "Design Patterns for Container-based Distributed Systems (Burns & Oppenheimer, USENIX HotCloud '16)"
    url: "https://www.usenix.org/conference/hotcloud16/workshop-program/presentation/burns"
  - title: "Design patterns for container-based distributed systems (Google Research publication page)"
    url: "https://research.google/pubs/design-patterns-for-container-based-distributed-systems/"
  - title: "Sharded Services (Bindu C, Medium)"
    url: "https://medium.com/@bindubc/sharded-services-68db32e03d80"
  - title: "Sharded Services — Introduction to Distributed Systems (Educative)"
    url: "https://www.educative.io/courses/introduction-to-distributed-systems-for-dummies/np/sharded-services"
---

**Gist.** A replicated service scales by cloning a container that holds either the same state or none, which fails as soon as the served state exceeds one machine: a cache with more keys than fit in random-access memory (RAM), an index larger than one disk, a leaderboard hotter than one CPU. The **sharded service** described by Burns in *Designing Distributed Systems* divides the state across replicas that are no longer interchangeable and places a **root** node in front that computes, per request, which replica owns the key. The cost is that the "any healthy node will do" invariant is gone: routing becomes a correctness concern, single-node failure darkens a slice of the keyspace, and every change in shard count implies data movement.

## Root and shards, not load balancer and replicas

In a sharded service each replica — a **shard** — serves a subset of requests. The root inspects the request, computes the owning shard, and forwards. The root performs real work rather than round-robin selection, and **a routing error is not a load imbalance but a data error**: a request for an existing key is answered as missing, or worse, two roots disagree and one shard's writes are split across two owners, after which neither holds the full history for that key.

| | Replicated service | Sharded service |
|---|---|---|
| Replicas | Identical, stateless or fully synced | Disjoint, each owns a slice of state |
| Router's job | Pick *any* healthy replica | Pick the *one correct* replica |
| Scales with | Request rate | Data volume (and request rate) |
| Failure of one node | Capacity dips | A slice of the keyspace goes dark |

The last row is the tax. Sharding buys capacity by trading away the safety net of interchangeability, so **each shard normally carries its own replication underneath** to survive node loss. Sharding and replication are orthogonal axes and are usually combined rather than chosen between: sharding divides the keyspace, replication duplicates a division.

## The sharding function

The root requires a deterministic, uniform mapping from request key to shard identifier. The direct form:

```scala
def shardFor(key: String, shardCount: Int): Int =
  val digest = MessageDigest.getInstance("SHA-1").digest(key.getBytes("UTF-8"))
  // floorMod, not %: the leading 8 digest bytes read as a signed Long are
  // negative half the time, and a negative index is not a shard.
  math.floorMod(ByteBuffer.wrap(digest, 0, 8).getLong, shardCount)
```

Two properties matter more than the choice of hash algorithm:

- **Determinism and uniformity.** The same key must always land on the same shard, and keys must spread evenly so that no shard is structurally overloaded by the mapping itself.
- **Key granularity.** The shard key must be coarse enough that related lookups land together, preserving locality, and fine enough that no single key's traffic can dominate a shard.

The `% shard_count` term is the function's principal liability: changing `shard_count` remaps nearly every key, because the residue of a fixed digest modulo a new divisor is unrelated to the old residue. Consistent hashing exists to bound that disruption to approximately `keys / shards` rather than the whole keyspace; it is derived in this journal's dedicated hash-ring article and is not re-derived here. The narrower point for this pattern is that **the resizing procedure must be designed before the first shard is deployed**, because the shard count will change.

## Hot shards

A uniform hash over keys does not produce uniform *load*, because request distributions over keys are not uniform. One viral post, one celebrity account, one popular stock-keeping unit can saturate a single shard's CPU or network link while its siblings idle. This is the **hot shard** problem, and it is structural to sharding: a replicated tier would have spread the same hot key across every replica, whereas a sharded tier concentrates it by construction.

Mitigations, ordered by how much of the architecture they disturb:

1. **Split the hot key further.** Sub-shard along a secondary dimension — time bucket, region — so no single shard absorbs the key alone.
2. **Replicate the hot shard rather than the tier.** Give the hot shard additional read replicas while cold shards remain single-instance. This composes sharding with replication selectively instead of uniformly.
3. **Relocate rather than add.** Move the hot shard onto dedicated hardware while colder shards continue to share nodes. This changes physical placement and **leaves the sharding function untouched**, so no keys move.

Detection precedes architecture: **per-shard p99 latency and per-shard queries per second (QPS)** are the required signals, because a single saturated shard among many is invisible in a tier-wide mean.

## Adding shards and rebalancing

Growth changes the shard count, which forces key movement. A sequence that keeps the service answerable throughout:

1. Start the new shards empty.
2. Update the sharding function, or the consistent-hash ring, so that newly written keys route to their new owner immediately.
3. Migrate existing keys lazily: on a miss at the new shard, consult the previous owner, return the value, and backfill the new shard.
4. When a background sweep confirms a key range has fully migrated, stop routing reads for that range to the old shard.

Step 3 is what converts a big-bang cutover into a rolling one. **Its cost is a temporary double-lookup path in the root**, so tail latency rises for the duration of the migration and the old shards must remain reachable until step 4 completes for every range. Omitting step 3 in a cache tier does not produce errors that are easy to notice: reads route to an empty new owner, miss, and repopulate from the origin, so the tier stays available while its hit rate collapses and origin load multiplies.

## A sharded cache in front of a service

The clearest concrete instance is a cache tier sharded in front of an origin service, with the root also acting as a scatter-gather coordinator for multi-key requests. A multi-key request touches several shards, and **its latency is bounded below by the slowest shard contacted, not the average** — the scatter-gather tax, which grows with the number of shards a single request must fan out to.

### Implementation sketch (Scala)

```scala
final class ShardedCache(shards: IndexedSeq[Shard], origin: Origin)(using
    ec: ExecutionContext):

  // Every root instance must derive the same owner for the same key;
  // disagreement here is the split-ownership failure, not a hiccup.
  private def owner(key: String): Int = shardFor(key, shards.size)

  def getOrFetch(key: String): Array[Byte] =
    val shard = shards(owner(key))
    shard.get(key).getOrElse:
      val value = origin.fetch(key)
      shard.set(key, value, ttlSeconds = 300)
      value

  def scatterGather(keys: Seq[String]): Map[String, Array[Byte]] =
    val byShard: Map[Int, Seq[String]] = keys.groupBy(owner)
    val replies = byShard.toSeq.map: (id, ks) =>
      Future(shards(id).mget(ks))
    // The gather completes only when the slowest shard replies.
    Await.result(Future.sequence(replies), 200.millis).flatten.toMap
```

Each shard can be a `StatefulSet`, whose ordinals give stable, addressable identity that the sharding function maps directly onto a pod:

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: cache-shard
spec:
  serviceName: cache-shard
  replicas: 4
  selector: { matchLabels: { app: cache-shard } }
  template:
    metadata: { labels: { app: cache-shard } }
    spec:
      containers:
        - name: redis
          image: redis:7
          ports: [{ containerPort: 6379 }]
```

`cache-shard-0.cache-shard` through `cache-shard-3.cache-shard` are stable DNS names the root can hash into directly. Growing capacity is `replicas: 4` → `replicas: 6` followed by the rebalance sequence above: **the StatefulSet supplies the new pods and migrates no keys.**

## Pitfalls

- Two root instances running different shard counts during a rolling deploy send writes for the same key to different shards; both shards then hold a partial, divergent history and neither read path can reconstruct it.
- Raising the modulus without the lazy-migrate step leaves the tier available but nearly every key resolves to an empty shard, so the symptom is a hit-rate collapse and multiplied origin load rather than an error.
- A tier-wide mean latency that looks healthy hides a shard pinned at 100% CPU by one hot key; only per-shard p99 and per-shard QPS separate the two.
- Choosing too coarse a shard key concentrates a single tenant or single popular entity onto one shard, which no rebalancing of shard *count* can relieve, because the unit of movement is the key.
- Decommissioning old shards before the background sweep confirms every range migrated drops every key not yet copied, since the fallback owner no longer exists; where the shard is a cache the cost is a miss to the origin, where it is the authoritative copy the loss is permanent.
- A scatter-gather request fanning out to all shards makes the tier's tail latency the maximum over shards, so adding shards to relieve capacity pressure degrades multi-key latency.
