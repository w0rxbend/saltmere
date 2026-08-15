---
title: "Replication Models: Single-Leader, Multi-Leader, Leaderless"
date: 2026-08-13
track: distributed-systems
summary: "A map of replication: single-leader with its synchronous, asynchronous and semi-synchronous acknowledgement points and lag pathologies, multi-leader conflict resolution and the data loss inherent in last-write-wins, and Dynamo-style leaderless replication with read repair, with a comparison table."
reading_time: 7
tags: [replication, single-leader, multi-leader, leaderless, consistency]
sources:
  - title: "Dynamo: Amazon's Highly Available Key-value Store — DeCandia et al. (SOSP 2007)"
    url: "https://www.allthingsdistributed.com/files/amazon-dynamo-sosp2007.pdf"
  - title: "Semisynchronous Replication (MySQL 8.4 Reference Manual)"
    url: "https://dev.mysql.com/doc/refman/8.4/en/replication-semisync.html"
  - title: "Mitigating Replication Lag and Reducing Read Load with freno (GitHub Engineering)"
    url: "https://github.blog/engineering/infrastructure/mitigating-replication-lag-and-reducing-read-load-with-freno/"
  - title: "Jepsen: Cassandra (Kyle Kingsbury, 2013)"
    url: "https://aphyr.com/posts/294-jepsen-cassandra"
  - title: "Designing Data-Intensive Applications, Chapter 5 Notes (timilearning)"
    url: "https://timilearning.com/posts/ddia/part-two/chapter-5/"
---

**Gist.** A replicated dataset must decide **which nodes are allowed to accept writes**: one node (single-leader), several coordinating nodes (multi-leader), or any replica (leaderless). Restricting writes to one node yields a total order and removes conflicts, but couples write availability and write latency to that node; admitting writes at several nodes preserves local availability and latency at the cost of concurrent versions that some merge function must reconcile. Every model therefore pays for its availability in consistency work performed somewhere else — at failover, at sync time, or at read time.

This article is the overview; the deep dives — [quorums](/articles/distributed-systems/2026-07-25-quorum-replication-r-plus-w), [hinted handoff](/articles/distributed-systems/2026-07-30-hinted-handoff-sloppy-quorums), [session guarantees](/articles/distributed-systems/2026-07-30-client-centric-consistency-session-guarantees), [CRDTs](/articles/distributed-systems/2026-07-26-crdts-conflict-free-replication), [chain replication](/articles/distributed-systems/2026-07-30-chain-replication-craq) — are linked where they belong.

## Single-leader: one write path, three acknowledgement points

All writes are applied at the leader, which ships an ordered change log to its followers (Postgres, MySQL, MongoDB, Kafka partitions). Because every write passes through one node, **the log order is a total order**, and no two replicas can hold genuinely concurrent versions of the same record: a follower is either current or behind. Reads may be served by followers, which is both the read-scaling mechanism and the source of every consistency anomaly below.

The variable is *when the leader acknowledges a commit to the client*:

- **Synchronous.** The leader waits for a follower to confirm before acknowledging. **No acknowledged write is lost if the leader then fails**, because at least one surviving replica holds it; the cost is that a single slow or dead follower stalls all writes, which is why replication to *every* replica synchronously is rarely configured.
- **Asynchronous.** The leader acknowledges immediately and replicates in the background. Write latency is independent of replica health, but a leader crash loses the tail of the log that had not yet been shipped, and a failover that promotes a lagging follower **discards writes that were already acknowledged to clients**.
- **Semi-synchronous.** The leader waits for at least one follower to confirm *receipt*. MySQL's semisynchronous replication waits for the transaction to reach a replica's relay log and be flushed to disk, **not** for the replica to apply it, and **falls back to asynchronous operation if no replica acknowledges within the configured timeout**. The guarantee is therefore durability against a single crash, without a dependency on the slowest replica — and it is a guarantee that the source itself may withdraw under timeout.

The distinction between *received* and *applied* is load-bearing: a semi-synchronously acknowledged write is durable on a second machine, yet a read served by that same replica may still not observe it.

**Replication lag pathologies.** Serving reads from followers reintroduces the anomalies that a total order was supposed to eliminate. A client reads its own write from a stale follower and the write appears absent (read-after-write). Two successive reads land on differently lagged replicas and observed time moves backward (monotonic reads). A reply replicates before the message it answers (consistent prefix). These are exactly the [session guarantees](/articles/distributed-systems/2026-07-30-client-centric-consistency-session-guarantees) and are named rather than re-derived here.

The production remedy is lag-aware routing. GitHub measures lag from a heartbeat timestamp written on the primary and read back from each replica, and exposes the result through **freno**, a central throttler that applications consult before bulk writes or replica reads rather than each deciding for itself:

```text
on write(user):        remember last_write_ts[user]
on read(user):
    lag = max over replicas of (now - heartbeat_ts)   # freno-style probe
    if now - last_write_ts[user] < lag:  route to primary   # read-after-write
    else:                                route to replica
```

Failover is the second failure mode. Promoting a stale asynchronous follower loses the writes it never received; promoting two candidates concurrently produces **split brain**, two nodes each accepting writes into what is nominally one total order. [Chain replication and CRAQ](/articles/distributed-systems/2026-07-30-chain-replication-craq) are the single-writer variant that recovers strong consistency for reads by serving them from the *tail* of the chain.

## Multi-leader: writes accepted in several places, conflicts owned

Several nodes each accept writes and replicate to one another asynchronously. Two deployments justify the added conflict handling: **multi-datacentre** setups, where each datacentre commits at local latency and continues to accept writes across an inter-datacentre partition, and **offline-capable clients**, where a device's local database acts as a leader and synchronises later — the model CouchDB is built around.

The cost is **write conflicts**: the same key modified concurrently at two leaders, with the conflict discovered only when the two logs meet. Resolution strategies, ordered by how much information they preserve:

- **Last-write-wins (LWW).** Retain the version with the highest timestamp and discard the others. The rule converges, and it is **lossy by construction**: wall clocks skew between leaders, so the surviving version need not be the causally later one, and equal timestamps require an arbitrary tie-break. Jepsen's 2013 Cassandra analysis demonstrated the loss directly: **concurrent updates to a last-write-wins register lost acknowledged writes at QUORUM**, because convergence is reached by dropping every version but one. No general bound on the loss rate follows from that test; the accurate summary is that LWW converges by discarding data.
- **Detect and retain siblings.** Track causality with version vectors, retain the concurrent versions, and expose them to the application for merging. This preserves information but moves the merge into application code and requires the application to handle an unbounded set of siblings.
- **Merge automatically.** [CRDTs](/articles/distributed-systems/2026-07-26-crdts-conflict-free-replication) define a commutative, associative and idempotent merge, so replicas converge without coordination and without discarding concurrent updates.

### Implementation sketch (Scala)

Sibling detection turns on comparing version vectors rather than timestamps. Two versions conflict when neither vector dominates the other; the write must then be retained alongside, not overwritten.

```scala
type Node = String
opaque type VClock = Map[Node, Long]

extension (a: VClock)
  def dominates(b: VClock): Boolean =
    b.forall((n, c) => a.getOrElse(n, 0L) >= c) && a != b
  def concurrentWith(b: VClock): Boolean =
    !a.dominates(b) && !b.dominates(a) && a != b
  def increment(n: Node): VClock =
    a.updated(n, a.getOrElse(n, 0L) + 1)

final case class Version[A](value: A, clock: VClock)

/** Retains siblings instead of resolving them: an incoming version replaces
  * only those it strictly dominates, and is kept beside anything concurrent. */
def merge[A](siblings: Set[Version[A]], incoming: Version[A]): Set[Version[A]] =
  if siblings.exists(_.clock.dominates(incoming.clock)) then siblings
  else siblings.filterNot(s => incoming.clock.dominates(s.clock)) + incoming
```

A last-write-wins store replaces the final line with a single `maxBy(_.timestamp)`, which is where the discarded updates go.

## Leaderless: quorums in place of a leader

Dynamo-style systems (Cassandra, Riak, Voldemort) remove the leader: a client or coordinator sends each write to all N replicas responsible for the key and treats it as committed once W acknowledge, and reads query R replicas and take the newest version returned. The [R + W > N arithmetic](/articles/distributed-systems/2026-07-25-quorum-replication-r-plus-w) and its caveats are treated separately, as are the availability extensions — [sloppy quorums and hinted handoff](/articles/distributed-systems/2026-07-30-hinted-handoff-sloppy-quorums).

The distinguishing property is **repair rather than prevention**. Replicas that missed a write are not blocked; they are corrected later by two mechanisms: **read repair**, where a read that observes divergent versions writes the newest back to the stale replicas, and background [anti-entropy with Merkle trees](/articles/distributed-systems/2026-07-27-merkle-trees-anti-entropy), which compares replica contents without transferring them in full. A key that is never read receives no read repair, so anti-entropy is what bounds divergence for cold data.

Dynamo tracked causality with [vector clocks](/articles/distributed-systems/2026-07-24-vector-clocks-in-40-lines) and returned concurrent versions to the application for merging — the shopping-cart union described in the SOSP 2007 paper. Cassandra adopted the Dynamo architecture but resolves with last-write-wins timestamps rather than vector clocks, and so inherits the loss behaviour described above.

## Comparison

| | Single-leader | Multi-leader | Leaderless |
|---|---|---|---|
| **Writes go to** | The one leader | Any leader (per DC/device) | Any N replicas via quorum |
| **Conflicts** | None (total order) | At sync time; must resolve | Concurrent versions; read repair + merge |
| **On node failure** | Failover (lag = lost writes, split-brain risk) | Other leaders keep working | Sloppy quorum / hinted handoff |
| **Latency profile** | Cross-region writes pay leader RTT | Local writes per region | Tunable via R, W |
| **Consistency** | Strong at leader; lag on followers | Eventual + merge | Tunable, eventual by default |
| **Examples** | Postgres, MySQL, Kafka, MongoDB | CouchDB, multi-DC MySQL/Postgres setups | Dynamo, Cassandra, Riak |

Single-leader is the default because it removes conflicts outright. Multi-leader is justified when geography or offline operation makes a single write path untenable *and* a merge function can be stated for the data. Leaderless is justified when write availability and latency dominate, and the application can absorb reconciliation at read time.

**Experiment.** Running MySQL with `rpl_semi_sync_source_wait_point=AFTER_SYNC` across two containers and killing the replica under load exposes the fallback path: the source's semisynchronous status flips to asynchronous once the timeout expires, and writes that were durable on two machines a moment earlier are durable on one.

## Pitfalls

- **Treating semi-synchronous acknowledgement as read-visible.** A transaction confirmed by a replica sits in its relay log; a read routed to that replica before it applies the log returns the older value.
- **Assuming semi-synchronous replication is always semi-synchronous.** After the acknowledgement timeout the source reverts to asynchronous operation, and the single-crash durability guarantee no longer holds until a replica catches up.
- **Promoting the most available follower during failover.** Availability is not recency; an asynchronous follower promoted while behind silently drops every acknowledged write it had not received.
- **Reading "LWW is eventually consistent" as "LWW is safe".** Convergence is achieved by discarding concurrent writes — Jepsen observed acknowledged writes lost against a Cassandra last-write-wins register at QUORUM.
- **Resolving multi-leader conflicts by timestamp across datacentres.** Clock skew between leaders means the retained version is the one with the larger clock reading, which need not be the causally later write.
- **Relying on read repair alone to bound divergence.** Read repair only fires on keys that are read; rarely read keys stay stale until anti-entropy runs, so a disabled or lagging repair job leaves permanent divergence.
- **Routing reads to a replica on a fixed staleness threshold.** A hard-coded delay assumes a lag ceiling that no mechanism enforces; a replica that falls further behind than the threshold serves stale reads without signalling it.
