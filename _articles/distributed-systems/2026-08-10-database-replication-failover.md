---
title: "Database replication and failover: three topologies and the lag they leak"
date: 2026-08-10
track: distributed-systems
summary: "Each read replica trades write throughput for a set of consistency anomalies. This article covers the three replication topologies, the durability-versus-latency knob of synchronous and asynchronous commit, the three lag anomalies and their fixes, and the failover steps that discard committed writes."
reading_time: 7
tags: [replication, failover, consistency, replication-lag, kleppmann]
sources:
  - title: "Kleppmann, Designing Data-Intensive Applications, Ch. 5 (Replication)"
    url: "https://dataintensive.net/"
  - title: "PostgreSQL Documentation: Server Configuration — Replication (synchronous_standby_names, synchronous_commit)"
    url: "https://www.postgresql.org/docs/current/runtime-config-replication.html"
  - title: "MySQL 8.4 Reference Manual: 19.4.10 Semisynchronous Replication"
    url: "https://dev.mysql.com/doc/refman/8.4/en/replication-semisync.html"
  - title: "Timilearning, DDIA Notes — Chapter 5: Replication"
    url: "https://timilearning.com/posts/ddia/part-two/chapter-5/"
---

**Gist.** Replication is the single place where scaling reads, surviving machine failure, and preserving correctness pull against one another. Adding a follower buys read throughput and a hot spare, but introduces *replication lag* — reads that observe a stale prefix of history — and a *failover* procedure that can discard acknowledged writes. Chapter 5 of Kleppmann's *Designing Data-Intensive Applications* (DDIA) organises the subject around three topologies; the cost each imposes is either conflict resolution or a hard leader-promotion step.

## The three topologies

**Single-leader.** One node accepts all writes and streams a replication log — the write-ahead log (WAL) in PostgreSQL, the binary log (binlog) in MySQL — to read-only followers. Reads scale horizontally; **writes do not, because every write funnels through one node**. The compensating property is that **a single node imposes a total order on writes**, so no two versions of a row can conflict. This is the default arrangement in PostgreSQL, MySQL and most managed relational databases, and it is the topology that requires a failover story.

**Multi-leader.** Several nodes accept writes and replicate to one another. The arrangement fits cases where one leader is geographically wrong (one leader per datacentre, so writes commit locally) or where a client is offline-capable — a phone or a calendar application acts as its own leader until it reconnects. The cost is **write conflicts**: two leaders can independently accept updates to the same row, and the system must reconcile them by last-write-wins, application-level merge, or conflict-free replicated data types (see [CRDTs](/articles/distributed-systems/2026-07-26-crdts-conflict-free-replication)). Conflict resolution is the whole of the difficulty in multi-leader.

**Leaderless.** No node is distinguished; a client or coordinator writes to several replicas and reads from several, in the style of Amazon's Dynamo. Correctness rests on **quorum overlap: with N replicas, W acknowledgements per write and R responses per read, `R + W > N` forces the read set to intersect the write set**, so at least one responding replica holds the latest version. Divergence is repaired by *read repair* (correcting stale replicas on the read path) and *anti-entropy* (a background reconciliation process, often Merkle-tree-based). Cassandra and Riak are built on this design, following Amazon's Dynamo paper; the quorum arithmetic is treated in [quorum replication](/articles/distributed-systems/2026-07-25-quorum-replication-r-plus-w).

| Topology | Writes | Conflicts? | Failover | Typical use |
|---|---|---|---|---|
| Single-leader | one node | none (single order) | promote a follower; tricky | Postgres, MySQL, most RDBMS |
| Multi-leader | many nodes | yes — must resolve | leaders are independent | multi-DC, offline clients |
| Leaderless | any replica | version conflicts | none needed (quorums) | Dynamo, Cassandra, Riak |

## Synchronous and asynchronous commit

Within single-leader replication the sharpest knob is *when* the leader treats a write as complete.

- **Synchronous.** The leader waits for a follower to confirm before acknowledging the client. If the leader then dies, that follower holds the write, so nothing acknowledged is lost. The cost is that **a stalled synchronous follower blocks every write**. Making all followers synchronous multiplies that exposure, so deployments use **semi-synchronous** replication: one follower synchronous, the rest asynchronous. PostgreSQL expresses the choice through `synchronous_standby_names` — `ANY 1 (s1, s2, s3)` for quorum-style waiting, `FIRST 2 (...)` for priority order — and `synchronous_commit`, whose levels `remote_apply`, `on`, `remote_write`, `local` and `off` select how far a write must propagate before commit returns. MySQL's semisynchronous replication offers `rpl_semi_sync_source_wait_point = AFTER_SYNC`, under which the source waits for a replica to acknowledge receipt before committing.
- **Asynchronous.** The leader acknowledges immediately and ships changes in the background. Write latency is lowest and availability highest, but **any write not yet replicated when the leader dies is lost**. This is the default for read-scaling deployments and the origin of the anomalies below.

The trade-off does not reduce: synchronous commit buys durability with latency and an availability risk, since a slow follower drags the leader; asynchronous commit buys latency and availability with a durability hole at failure time. Asynchronous replication is the "else, latency" branch of [CAP/PACELC](/articles/distributed-systems/2026-08-10-cap-theorem-pacelc).

## Replication lag and three anomalies

Under asynchronous replication a follower always trails the leader. Under load or network disturbance the gap widens from milliseconds to seconds or longer, and follower reads begin returning states that violate a reader's expectations. Three anomalies follow, each with a routing fix; they are the read-side view of [client-centric consistency](/articles/distributed-systems/2026-07-30-client-centric-consistency-session-guarantees).

**1. Read-after-write.** A client posts a comment (write to the leader), reloads (read from a lagging follower), and the comment is absent. The write is not lost; the follower has not applied it. **Fix:** for a window after a write, route that client's reads of the affected data to the leader, or to a replica known to have applied the write.

**2. Monotonic reads.** Two successive reads land on different followers. The first, a fresh one, shows the comment; the second, a staler one, does not — the client observes time running backwards. **Fix:** pin each client to a single replica, for example by hashing the user identifier, so the observed state only advances.

**3. Consistent prefix reads.** An observer sees an answer before the question it answers, because causally ordered writes fell on different partitions that replicate at different rates. **Fix:** place causally related writes on the same partition, or carry causal dependencies explicitly.

### Implementation sketch (Scala)

The routing fix is small. The version below tracks the leader's log sequence number (LSN) rather than elapsed time: a read may be served by a follower only if that follower reports having replayed to at least the LSN produced by the client's last write. That converts a guess about lag into a checkable condition.

```scala
type Lsn = Long

trait Leader:
  def put(key: String, value: String): Lsn   // returns the commit LSN
  def get(key: String): Option[String]

trait Follower:
  def replayedTo(): Lsn                      // last LSN applied locally
  def get(key: String): Option[String]

final class Router(leader: Leader, followers: IndexedSeq[Follower]):
  // last write position observed per client; empty means "no recent write"
  private var watermark: Map[String, Lsn] = Map.empty

  def write(userId: String, key: String, value: String): Unit =
    watermark = watermark.updated(userId, leader.put(key, value))

  def read(userId: String, key: String): Option[String] =
    val required = watermark.getOrElse(userId, 0L)
    val follower = pick(userId)
    // read-after-write: fall back to the leader when the follower is behind
    if follower.replayedTo() >= required then follower.get(key)
    else leader.get(key)

  // monotonic reads: one client always lands on the same follower
  private def pick(userId: String): Follower =
    followers(math.floorMod(userId.hashCode, followers.size))
```

The anomaly the check prevents, stated as a trace of the naive "always read a follower" variant:

```
t=0   client -> leader.put("bio", "hi")      # leader acknowledges immediately (async)
t=0   leader -> follower  (replication in flight)
t=1   client -> follower.get("bio") -> None  # read-after-write violation
```

A time-based staleness budget approximates the same condition but cannot verify it; the LSN comparison is a direct test of whether the follower has the write.

## Failover: where acknowledged writes disappear

When the leader fails, a single-leader system must promote a follower. Each step of that procedure carries a distinct hazard (DDIA, Ch. 5).

- **Detecting failure.** Detection is normally a timeout. Too long a timeout delays recovery; too short a timeout triggers failovers during transient network disturbance, which can be more damaging than the condition it responds to.
- **Choosing a new leader.** The most up-to-date follower minimises loss, but **under asynchronous replication no follower holds everything**, so promotion decides how much to lose rather than whether to lose anything.
- **Discarded writes.** Writes the old leader acknowledged but had not replicated are dropped at promotion. If identifiers from those writes escaped to other systems — a cache, a message queue — those systems retain dangling references. GitHub reported this outcome: a promoted MySQL replica was behind, primary keys were reused, and private data was exposed across accounts.
- **Split-brain.** The old leader was unreachable rather than dead, and resumes believing it still holds leadership while the new leader also accepts writes. Two leaders diverge. A mechanism must force one down — shoot the other node in the head (STONITH) — and must not terminate both.
- **Stale leadership beliefs.** A node paused by garbage collection or partitioned from its peers may act on an expired leadership grant. The correction is a **fencing token: a monotonically increasing number issued with each leadership grant, which the storage layer records; any write carrying a token lower than the highest already seen is rejected**. The token is what makes a lease enforceable at the storage layer rather than merely advisory, and it is why leadership grants are commonly delegated to a consensus layer such as [Raft](/articles/distributed-systems/2026-07-26-raft-consensus-in-practice) rather than to a heartbeat script.

The summary: single-leader replication supplies a clean write order at the price of a hard failover; multi-leader and leaderless replication avoid failover and substitute conflict resolution. Asynchronous replication, which makes read scaling inexpensive, is the same mechanism that discards writes at failover and produces every lag anomaly above.

## Pitfalls

- Making every follower synchronous converts each follower's stall into a global write stall; with one synchronous follower the leader blocks only on that follower.
- A time-based staleness window silently fails whenever real lag exceeds it: a follower trailing longer than the window serves a read-after-write violation, and no error is raised.
- Load-balancing a client's reads across followers breaks monotonic reads — the state a client observes can move backwards between two consecutive requests.
- Shortening the failure-detection timeout to speed recovery causes promotions during transient network disturbance, each of which discards the unreplicated tail of the old leader's log.
- Identifiers that were assigned by discarded writes may be reissued after promotion; systems holding the old identifiers then resolve them to unrelated rows, as in the GitHub incident above.
- An unreachable leader is not a dead leader: without a fencing token checked at the storage layer, a recovered or unpaused old leader can still commit writes after a new one has been promoted.
- Under leaderless replication, `R + W > N` guarantees set overlap, not recency of the value returned unless versions are compared; read repair and anti-entropy remain necessary to converge replicas that were absent during a write.
