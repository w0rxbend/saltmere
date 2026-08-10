---
title: "Database replication and failover: three topologies and the lag that bites you"
date: 2026-08-10
track: distributed-systems
summary: "Every read replica you add trades write throughput for a fresh set of consistency anomalies. Here are the three replication topologies, the durability-vs-latency knob of sync vs async, the three lag anomalies and their fixes, and why failover is the part that loses your data."
reading_time: 6
tags: [replication, failover, consistency, replication-lag, kleppmann]
sources:
  - title: "Kleppmann, Designing Data-Intensive Applications, Ch. 5 (Replication)"
    url: "https://dataintensive.net/"
  - title: "PostgreSQL Documentation: 19.6 Replication (synchronous_standby_names, synchronous_commit)"
    url: "https://www.postgresql.org/docs/current/runtime-config-replication.html"
  - title: "MySQL 8.4 Reference Manual: 19.4.10 Semisynchronous Replication"
    url: "https://dev.mysql.com/doc/refman/8.4/en/replication-semisync.html"
  - title: "Timilearning, DDIA Notes — Chapter 5: Replication"
    url: "https://timilearning.com/posts/ddia/part-two/chapter-5/"
---

Replication is the load-bearing wall of every system-design interview, and the reason is simple: it's the one place where scaling reads, surviving machine failure, and keeping data correct all pull against each other at once. Add a follower and you get read throughput and a hot spare — but you also inherit *replication lag* and, eventually, a *failover* that can silently drop committed writes. Chapter 5 of Kleppmann's DDIA frames the whole subject around three topologies and the anomalies each one leaks. This is that framing, made concrete.

## The three topologies

**Single-leader.** One node accepts all writes and streams a replication log (WAL in Postgres, binlog in MySQL) to read-only followers. Reads scale horizontally; writes do not, because every write funnels through the one leader. This is the default for Postgres, MySQL, and most managed relational databases. It's simple to reason about — there's a single ordering of writes — but the leader is a write bottleneck and a failure point, so you need a failover story.

**Multi-leader.** Several nodes accept writes and replicate to each other. You reach for this when a single leader is geographically wrong: one leader per datacenter for lower write latency, or an offline-capable client (a phone, a calendar app) that is effectively its own leader until it reconnects. The price is *write conflicts* — two leaders can independently accept conflicting updates to the same row — and you must resolve them (last-write-wins, application merge, or CRDTs; see [CRDTs](/articles/distributed-systems/2026-07-26-crdts-conflict-free-replication)). Conflict resolution is the entire difficulty of multi-leader.

**Leaderless.** No node is special; the client (or a coordinator) writes to several replicas and reads from several, Dynamo-style. Correctness comes from *quorums*: if the read set and write set overlap, a read is guaranteed to see the latest write. Staleness is repaired by *read-repair* (fix stale replicas on the read path) and *anti-entropy* (a background process, often Merkle-tree-based, that reconciles replicas). This is Cassandra, Riak, and DynamoDB's internals. The quorum arithmetic — `R + W > N` — is a tunable knob covered in depth in [quorum replication](/articles/distributed-systems/2026-07-25-quorum-replication-r-plus-w).

| Topology | Writes | Conflicts? | Failover | Typical use |
|---|---|---|---|---|
| Single-leader | one node | none (single order) | promote a follower; tricky | Postgres, MySQL, most RDBMS |
| Multi-leader | many nodes | yes — must resolve | leaders are independent | multi-DC, offline clients |
| Leaderless | any replica | version conflicts | none needed (quorums) | Dynamo, Cassandra, Riak |

## Synchronous vs asynchronous: durability against latency

Within single-leader, the sharpest knob is *when* the leader considers a write done.

- **Synchronous:** the leader waits for a follower to confirm before acking the client. If the leader dies, that follower has the data — no loss. But if the synchronous follower stalls, *all writes block*. Making every follower synchronous is impractical, so real systems use **semi-synchronous**: exactly one follower is synchronous, the rest async. Postgres expresses this with `synchronous_standby_names` (e.g. `ANY 1 (s1, s2, s3)` for quorum-style, or `FIRST 2 (...)` for priority) and `synchronous_commit`, whose levels — `remote_apply`, `on`, `remote_write`, `local`, `off` — let you dial exactly how far a write must propagate before commit returns. MySQL's equivalent is semisync with `rpl_semi_sync_source_wait_point = AFTER_SYNC`, where the source waits for a replica to acknowledge receipt before committing.
- **Asynchronous:** the leader acks immediately and ships changes in the background. Lowest write latency, best availability — but any write not yet replicated when the leader dies is **lost**. This is the default for most read-scaling deployments, and it's the source of nearly every replication headache below.

The trade-off is irreducible: synchronous buys durability with latency and an availability risk (a slow follower drags the leader); asynchronous buys latency and availability with a durability hole on failure. See also [CAP/PACELC](/articles/distributed-systems/2026-08-10-cap-theorem-pacelc) — async replication is exactly the "else, latency" branch.

## Replication lag and the three anomalies

With async replication a follower is always slightly behind. Under load or a network hiccup, "slightly" becomes seconds or minutes — and reads from followers start returning results that *look wrong to a human*. Three anomalies, three fixes (these are the read-side of [client-centric consistency](/articles/distributed-systems/2026-07-30-client-centric-consistency-session-guarantees)):

**1. Read-your-writes.** You post a comment (write → leader), then reload (read → a lagging follower) and your comment is gone. It's not gone — the follower hasn't caught up. Fix: after a write, route *that user's* reads for the affected data to the leader (or to a replica known to be caught up) for a window.

**2. Monotonic reads.** You refresh twice; the first read hits a fresh follower and shows the comment, the second hits a staler follower and it *disappears* — time moving backward. Fix: pin each user to the *same* replica (e.g. hash the user ID to a replica), so their view only ever moves forward.

**3. Consistent prefix reads.** An observer sees an answer before the question it answers, because causally-ordered writes landed on different partitions that replicate at different speeds. Fix: keep causally-related writes on the same partition, or track causal dependencies explicitly.

## Read-your-writes routing, concretely

The routing fix is a few lines. Record when the user last wrote; if a read falls inside the staleness window, send it to the leader.

```python
import time

REPLICATION_LAG_BUDGET = 10.0  # seconds we assume a follower may trail

class Router:
    def __init__(self, leader, followers):
        self.leader = leader
        self.followers = followers
        self.last_write = {}  # user_id -> monotonic timestamp

    def write(self, user_id, key, value):
        self.leader.put(key, value)           # all writes go to the leader
        self.last_write[user_id] = time.monotonic()

    def read(self, user_id, key):
        wrote_at = self.last_write.get(user_id, 0.0)
        if time.monotonic() - wrote_at < REPLICATION_LAG_BUDGET:
            return self.leader.get(key)        # read-your-writes: hit the leader
        return self._pick_follower(user_id).get(key)

    def _pick_follower(self, user_id):
        # monotonic reads: same user -> same follower, so their view never rewinds
        return self.followers[hash(user_id) % len(self.followers)]
```

And the anomaly this prevents, made explicit — the naive "always read a follower" version:

```
t=0   client -> leader.put("bio", "hi")     # leader acks immediately (async)
t=0   leader -> follower  (replication in flight, ~200ms+ behind)
t=1   client -> follower.get("bio") -> None  # BUG: read-your-writes violation
```

The `REPLICATION_LAG_BUDGET` is a guess; the honest version tracks the leader's log position (an LSN in Postgres) at write time and only serves from a follower that has replayed *past* that position. That turns a time-based guess into a correctness guarantee.

## Failover: where the data actually goes missing

When the leader dies, single-leader systems must promote a follower. Every step is a hazard (DDIA, Ch. 5):

- **Detecting failure.** Usually a timeout. Too long delays recovery; too short triggers needless failovers during a transient network blip — which is often *worse* than the original outage.
- **Choosing a new leader.** You want the most up-to-date follower to minimize loss, but under async replication no follower has *everything*, so promotion is a choice about *how much* to lose, not whether.
- **Lost writes.** Writes the old leader acked but hadn't replicated are discarded on promotion. If those IDs leaked to other systems (a cache, a message queue), you now have dangling references. GitHub had exactly this: a promoted MySQL replica was behind, primary keys got reused, and private data leaked across accounts.
- **Split-brain.** The old leader wasn't dead — just unreachable — and comes back thinking it's still leader while the new one is also accepting writes. Two leaders, diverging data. You need a mechanism to force one down (STONITH), and it must not accidentally kill both.
- **The fencing problem.** A node paused (GC, network) may act on a stale belief that it's leader. The fix is a **fencing token**: a monotonically increasing number issued on each leadership grant; the storage layer rejects any write carrying a token older than the highest it has seen. This is what makes a lease safe rather than merely hopeful, and it's why serious systems delegate leader election to a consensus layer like [Raft](/articles/distributed-systems/2026-07-26-raft-consensus-in-practice) rather than a heartbeat script.

The interview-grade summary: single-leader gives you a clean write order at the cost of a hard failover; multi-leader and leaderless dodge failover but hand you conflict resolution instead. And async replication — the thing that makes read scaling cheap — is the same thing that loses writes on failover and produces every lag anomaly above. There is no free replica.

**Try next:** Take the `Router` above and replace the time-based `REPLICATION_LAG_BUDGET` with LSN tracking: have `write` return the leader's log position, store it per user, and make `read` fall through to the leader unless the chosen follower reports a replayed position `>=` that LSN. Then simulate a failover — kill the leader mid-window, promote a follower that's 3 writes behind, and assert exactly which writes vanish. Add a fencing token and prove the old leader's late write gets rejected.
