---
title: "Replication Models: Single-Leader, Multi-Leader, Leaderless"
date: 2026-08-13
track: distributed-systems
summary: "The interview map of replication: single-leader with sync/async/semi-sync trade-offs and lag pathologies, multi-leader conflict resolution and why last-write-wins loses data, and Dynamo-style leaderless with read repair — plus the comparison table that ties the corpus together."
reading_time: 5
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

Every replication design answers one question: **who is allowed to accept writes?** One node (single-leader), several coordinating nodes (multi-leader), or any replica (leaderless). Each answer buys availability or simplicity and pays in consistency somewhere else. This is the overview; the deep dives — [quorums](/articles/distributed-systems/2026-07-25-quorum-replication-r-plus-w), [hinted handoff](/articles/distributed-systems/2026-07-30-hinted-handoff-sloppy-quorums), [session guarantees](/articles/distributed-systems/2026-07-30-client-centric-consistency-session-guarantees), [CRDTs](/articles/distributed-systems/2026-07-26-crdts-conflict-free-replication), [chain replication](/articles/distributed-systems/2026-07-30-chain-replication-craq) — are linked where they belong.

## Single-leader: one write path, three durability dials

All writes go to the leader, which ships an ordered change log to followers (Postgres, MySQL, MongoDB, Kafka partitions). Reads can be served by followers — that's the scaling story and the consistency problem.

The dial is *when the leader acknowledges commit*:

- **Synchronous**: wait for a follower to confirm. No acknowledged write is lost on leader failure, but one slow or dead follower blocks all writes — nobody runs fully sync across all replicas.
- **Asynchronous**: acknowledge immediately, replicate in the background. Fast and available; a leader crash loses the unreplicated tail, and a failover can silently discard acknowledged writes.
- **Semi-synchronous**: wait for at least one follower to confirm *receipt* — MySQL's semisync waits for the transaction to hit a replica's relay log and flush to disk, **not** for it to be applied, and falls back to async if no replica acknowledges within a timeout. Durability against a single crash, without waiting on the slowest replica.

**Replication lag pathologies** are the interview meat: read your own write from a stale follower and your comment "disappears" (read-your-writes); two successive reads hit differently-lagged replicas and time runs backward (monotonic reads); an answer replicates before its question (consistent prefix). These are exactly the [session guarantees](/articles/distributed-systems/2026-07-30-client-centric-consistency-session-guarantees) — name them, don't re-derive them. In production the fix is often explicit lag-aware routing. GitHub measures lag with a 100 ms `pt-heartbeat` timestamp on the primary and runs **freno**, a central throttler apps consult before bulk writes or replica reads — replacing an older heuristic of "only read from a replica if your last write was >5 s ago":

```text
on write(user):        remember last_write_ts[user]
on read(user):
    lag = max over replicas of (now - heartbeat_ts)   # freno-style probe
    if now - last_write_ts[user] < lag:  route to primary   # read-your-writes
    else:                                route to replica
```

Failover is the other classic follow-up: promote a stale async follower and you lose writes; promote two and you have split brain. (Chain replication is the single-writer family member that gets strong consistency by making the *tail* serve reads — see [chain replication & CRAQ](/articles/distributed-systems/2026-07-30-chain-replication-craq).)

## Multi-leader: accept writes in many places, own the conflicts

Each of several nodes accepts writes and asynchronously replicates to the others. Two legitimate use cases: **multi-datacenter** deployments (each DC writes locally at local latency, survives inter-DC partition) and **offline-capable clients** (your calendar app's device database is a "leader" that syncs later; CouchDB is built around this model).

The price is **write conflicts**: the same key modified concurrently on two leaders, discovered only at sync time. Options, roughly worst to best:

- **Last-write-wins (LWW)**: keep the highest timestamp, silently drop the rest. Convergent, but *lossy by design* — wall clocks skew, and equal timestamps force arbitrary tie-breaks. Jepsen's Cassandra analysis made this concrete: in one test of LWW registers under contention, ~28% of *acknowledged* writes were lost even at QUORUM with synchronized clocks. Say "LWW converges by discarding data" and you've passed this question.
- **Detect and keep siblings**: track causality (version vectors), surface concurrent versions to the application to merge.
- **Merge automatically**: [CRDTs](/articles/distributed-systems/2026-07-26-crdts-conflict-free-replication) make merge commutative so replicas converge without coordination.

## Leaderless: quorums instead of a boss

Dynamo-style systems (Cassandra, Riak, Voldemort) drop the leader entirely: a client (or coordinator) sends each write to all N home replicas and considers it committed after W acks; reads ask R replicas and take the newest version. The [R + W > N arithmetic](/articles/distributed-systems/2026-07-25-quorum-replication-r-plus-w) and its caveats are covered elsewhere, as are the availability add-ons — [sloppy quorums and hinted handoff](/articles/distributed-systems/2026-07-30-hinted-handoff-sloppy-quorums).

What's distinctive here is **repair instead of prevention**. Stale replicas are fixed lazily: **read repair** (a read that observes divergent versions writes the newest one back to the stale replicas) plus background [anti-entropy with Merkle trees](/articles/distributed-systems/2026-07-27-merkle-trees-anti-entropy). Dynamo tracked causality with [vector clocks](/articles/distributed-systems/2026-07-24-vector-clocks-in-40-lines) and pushed merges to the application — the famous shopping-cart union. Note the irony: Cassandra took Dynamo's architecture but swapped vector clocks for LWW timestamps, importing every LWW danger above.

## The comparison table

| | Single-leader | Multi-leader | Leaderless |
|---|---|---|---|
| **Writes go to** | The one leader | Any leader (per DC/device) | Any N replicas via quorum |
| **Conflicts** | None (total order) | At sync time; must resolve | Concurrent versions; read repair + merge |
| **On node failure** | Failover (lag = lost writes, split-brain risk) | Other leaders keep working | Sloppy quorum / hinted handoff |
| **Latency profile** | Cross-region writes pay leader RTT | Local writes per region | Tunable via R, W |
| **Consistency** | Strong at leader; lag on followers | Eventual + merge | Tunable, eventual by default |
| **Examples** | Postgres, MySQL, Kafka, MongoDB | CouchDB, multi-DC MySQL/Postgres setups | Dynamo, Cassandra, Riak |

Interview closer: pick single-leader by default; go multi-leader only when geography or offline operation forces it and you can name your merge function; go leaderless when write availability and latency matter more than read-time simplicity.

**Try next:** run MySQL with `rpl_semi_sync_source_wait_point=AFTER_SYNC` in two containers, kill the replica mid-load, and watch the source's semisync status flip to asynchronous after the timeout — the availability/durability dial in action.
