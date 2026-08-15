---
title: "Quorum replication: the R + W > N inequality"
date: 2026-07-25
track: distributed-systems
summary: "Vector clocks detect that a conflict happened. Quorums prevent most conflicts from arising, through a single inequality tuned per deployment. The rule, its arithmetic, and a simulation of the overlap property."
reading_time: 6
tags: [replication, quorum, consistency, availability, van-steen]
sources:
  - title: "van Steen & Tanenbaum, Distributed Systems (4th ed.), §7.5 Replication protocols"
    url: "https://www.distributed-systems.net/index.php/books/ds4/"
  - title: "Gifford, Weighted Voting for Replicated Data (SOSP 1979)"
    url: "https://dl.acm.org/doi/10.1145/800215.806583"
  - title: "DeCandia et al., Dynamo: Amazon's Highly Available Key-value Store (SOSP 2007)"
    url: "https://www.allthingsdistributed.com/files/amazon-dynamo-sosp2007.pdf"
---

**Gist.** Once a write-write conflict can be *detected* with vector clocks, the remaining problem is avoiding most conflicts without serialising every operation through a global lock. Quorum-based replication, described in chapter 7 of van Steen & Tanenbaum and introduced as weighted voting by Gifford (SOSP 1979), stores an object on `N` replicas and requires each read and each write to contact overlapping subsets, so that **every read set intersects the most recent write set**. The cost is that each operation now waits on several replicas rather than one, and the availability of reads and of writes moves in opposite directions as the quorum sizes are tuned.

## The rule

Every object is stored on `N` replicas. An operation contacts a *quorum* rather than all of them:

- a write must be acknowledged by `W` replicas,
- a read must collect responses from `R` replicas.

`W` and `R` are chosen so that:

```
R + W > N      # read quorum and write quorum always overlap
W > N / 2      # two writes cannot both succeed on disjoint sets
```

The first inequality carries the guarantee. If `R + W > N`, a read set of size `R` and a write set of size `W` cannot be disjoint: were they disjoint their union would require `R + W` distinct replicas, more than the `N` that exist, so **by the pigeonhole principle at least one replica belongs to both**. That shared replica has already applied the latest committed write, so its copy is present among the read responses. Each copy carries a version — a vector clock or a monotonic counter — and the reader selects the newest of the `R` responses. Read-after-write consistency for a completed write therefore holds without contacting every node.

The second inequality is a separate property. `R + W > N` says a reader sees a completed write; `W > N / 2` says two write quorums also overlap, so two concurrent writes cannot each succeed on a set the other never touched. Without it, `W = 1` on `N = 3` permits two writes to land on two different replicas with no replica witnessing both, and **the conflict is only discoverable later, on a read that happens to reach both**.

## The arithmetic is the design knob

The same inequality yields a slider between latency and consistency:

| N | W | R | Behavior |
|---|---|---|----------|
| 3 | 2 | 2 | Balanced. Tolerates 1 node down for both reads and writes. |
| 3 | 3 | 1 | Fast reads, `ROWA` writes — any node down blocks writes. |
| 3 | 1 | 3 | Fast writes, slow reads. A write survives 2 nodes being down. |
| 3 | 1 | 1 | `R + W = 2 ≯ 3`: **not** a strict quorum. Lowest latency, may read stale. |

The tolerance figures follow directly: an operation needing `k` responses survives `N − k` unreachable replicas, so **raising `W` for write-side consistency lowers write availability by exactly the same count**. Read-one-write-all (`ROWA`, row 2) is the extreme case — reads reach any single replica, and one unreachable replica stops all writes.

Rows 3 and 4 also violate `W > N / 2`, so write quorums no longer overlap even where the read guarantee survives. The last row abandons the overlap guarantee entirely in exchange for the lowest latency and the highest availability.

Dynamo exposes the `(N, R, W)` triple so that each service instance selects its own point. The paper reports `(3, 2, 2)` as the configuration common to several instances, and describes a "high performance read engine" pattern in which services set `R = 1` and `W = N`.

### Implementation sketch (Scala)

The overlap property is checkable directly. Replicas are modelled as `(value, version)` pairs; a write mutates an arbitrary `W`-subset, a read samples an arbitrary `R`-subset and takes the highest version.

```scala
import scala.util.Random

final case class Copy(value: Option[String], version: Long)

final class Store(n: Int):
  private val replicas = Array.fill(n)(Copy(None, 0L))

  private def sample(k: Int): Set[Int] =
    Random.shuffle((0 until n).toList).take(k).toSet

  def write(value: String, w: Int): Set[Int] =
    val version = replicas.map(_.version).max + 1
    val targets = sample(w)
    targets.foreach(i => replicas(i) = Copy(Some(value), version))
    targets

  def read(r: Int): (Copy, Set[Int]) =
    val responders = sample(r)
    // the newest of R responses; correct only if some responder saw the write
    (responders.map(i => replicas(i)).maxBy(_.version), responders)

@main def overlap(): Unit =
  val store = Store(5)
  val written = store.write("v2", w = 3)
  val (copy, answered) = store.read(r = 3)   // R + W = 6 > N = 5
  assert((written & answered).nonEmpty)      // cannot fail
```

With `N = 5, W = 3, R = 3` the assertion cannot fail: **the intersection is non-empty for every possible pair of subsets**, not merely for the ones the random sampler happens to draw. Lowering both to 2 gives `R + W = 4 ≯ 5`, and repeated iterations produce disjoint sets — a reproducible stale read, and the reason the inequality is a guarantee rather than a heuristic.

## Where the guarantee stops

A strict quorum is not linearizability. Three gaps remain:

- **Concurrent writes still produce siblings.** The overlap guarantees a reader *sees* the conflicting versions; it does not order them. Version metadata is what turns the sighting into a detectable conflict, which is why the version travels with every copy.
- **A coordinator crash mid-write leaves `W` partially applied.** Fewer than `W` replicas acknowledged, so the write never completed, yet the replicas that did apply it retain the value. A later read may return it.
- **Sloppy quorums relax membership.** Dynamo, under partition, accepts a write on replicas outside the object's designated set and records a hint so the value is handed off to the intended replica once it is reachable. The `W` acknowledgements are then not `W` acknowledgements *from the preference list*, so the pigeonhole argument no longer applies to the intended set. Writability is preserved; the clean overlap proof is not.

The inequality is the mental model. Production systems are the inequality plus these caveats.

## Pitfalls

- Configuring `R + W > N` while running sloppy quorums with hinted handoff: reads still return stale values, because the acknowledging replicas during a partition are not the replicas the read set is drawn from.
- Treating `R + W > N` as linearizability: two concurrent writes both satisfy the inequality and both survive, so a read returns siblings rather than a single latest value.
- Setting `W = 1` to reduce write latency: `W > N / 2` is violated, so two writes can land on disjoint replicas and neither coordinator observes the other.
- Raising `W` to `N` for stronger writes (`ROWA`): a single unreachable replica makes every write fail, since no write quorum can be assembled.
- Comparing copies by wall-clock timestamp instead of a version: clock skew across replicas can make an older copy compare as newer, and the read then discards the committed write it correctly retrieved.
- Assuming a partially applied write is invisible: the coordinator crashed before collecting `W` acknowledgements, but the replicas that applied the value serve it to subsequent reads.
