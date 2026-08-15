---
title: "Kafka Replication Internals: ISR, High Watermark, and the Limits of acks=all"
date: 2026-08-13
track: microservices
summary: "How Kafka commits a write: per-partition leaders, the in-sync replica set, high watermark versus log end offset, and why acks=all with the default min.insync.replicas=1 degrades to acks=1. Includes leader epochs (KIP-101) and Eligible Leader Replicas (KIP-966), on by default since 4.1."
reading_time: 7
tags: [kafka, replication, isr, high-watermark, durability]
sources:
  - title: "Replication — Apache Kafka design documentation"
    url: "https://kafka.apache.org/documentation/#replication"
  - title: "KIP-101 — Alter Replication Protocol to use Leader Epoch rather than High Watermark for Truncation"
    url: "https://cwiki.apache.org/confluence/display/KAFKA/KIP-101+-+Alter+Replication+Protocol+to+use+Leader+Epoch+rather+than+High+Watermark+for+Truncation"
  - title: "KIP-966: Eligible Leader Replicas"
    url: "https://cwiki.apache.org/confluence/display/KAFKA/KIP-966:+Eligible+Leader+Replicas"
  - title: "Kafka KIP-966 — Fixing the Last Replica Standing Issue (Jack Vanlightly)"
    url: "https://jack-vanlightly.com/blog/2023/8/17/kafka-kip-966-fixing-the-last-replica-standing-issue"
  - title: "The High Watermark Offset in Apache Kafka (2 Minute Streaming)"
    url: "https://blog.2minutestreaming.com/p/kafka-high-watermark-offset"
---

**Gist.** Apache Kafka replicates each partition to several brokers, and a record counts as durable only once it reaches a set of replicas whose membership changes at runtime — the in-sync replica set (ISR). The commit rule and the high watermark derived from it define exactly which records survive a leader failure. The cost is that the ISR shrinks under load or failure, so the producer acknowledgement setting `acks=all` weakens as the cluster degrades unless a second setting, `min.insync.replicas`, is configured to reject writes instead.

The protocol below has been the core of Kafka replication since leader epochs landed in 0.11, with KIP-966's Eligible Leader Replicas as the most recent addition.

## One leader per partition

Replication happens per partition, not per topic. Each partition has **one leader replica that serves all produce requests** (and, by default, consumer fetches); the other replicas are followers that replicate by issuing fetch requests to the leader, in the same manner as consumers. Every replica tracks a **log end offset (LEO)** — one past the last record in its local log. A follower's fetch request carries its own LEO, which is how the leader learns how far each follower has progressed.

## The ISR: Kafka's definition of "caught up"

The leader maintains the **in-sync replica set (ISR)**: itself plus every follower that has fetched up to the leader's log end within `replica.lag.time.max.ms` (default 30 s). A follower that lags longer than that is removed by the leader, and the membership change is persisted through the KRaft controller; a follower that catches up again is re-added.

The invariant everything else hangs off: **a record is committed once every member of the *current* ISR holds it.** Kafka's guarantee is that committed records are not lost as long as at least one ISR member survives. The qualifier *current* matters, because ISR membership is dynamic — that is the loophole behind the `acks=all` behaviour described below.

## High watermark versus log end offset

The **high watermark (HW)** marks the committed prefix of the log and is the minimum LEO across the ISR. Two consequences follow directly:

- **Consumers may read only up to the HW.** Records between the HW and the leader's LEO exist on the leader but are uncommitted, and may disappear on failover. Making them invisible means a consumer never observes a record that a later leader election erases.
- **Followers learn the HW from fetch responses**, so a follower's HW trails the leader's by at least one fetch round trip. That lag is small but not zero, and it was the source of log-divergence bugs before leader epochs (below).

The HW advances only when the slowest ISR member advances. A follower that is inside the lag window but persistently behind therefore holds back consumer visibility for the whole partition without ever being ejected.

## What acks=0/1/all guarantee

- `acks=0` — no broker confirmation of any kind.
- `acks=1` — the leader appended the record to its own log. The append reaches the operating system page cache, not necessarily disk: **Kafka's durability model is replication, not fsync.** The record is lost if the leader fails before any follower fetches it.
- `acks=all` (the producer default since Kafka 3.0, a consequence of idempotence-by-default in KIP-679) — the leader responds only after every replica *currently in the ISR* has the record.

The italicised clause is the trap. If two of three replicas fall behind and are ejected, the ISR contains the leader alone, and `acks=all` is then satisfied by the leader alone. **The setting weakens to `acks=1` precisely when the cluster is unhealthy**, and it does so without producing an error.

## min.insync.replicas: the second half of the setting

`min.insync.replicas` (default 1) supplies the missing floor: the leader **rejects `acks=all` produce requests with `NotEnoughReplicasException` whenever the ISR is smaller than the configured minimum**. The exception is retriable, which converts a silent durability loss into a visible, recoverable failure. Durability therefore requires configuring both sides:

```properties
# Topic/broker side
replication.factor=3
min.insync.replicas=2
unclean.leader.election.enable=false

# Producer side
acks=all
enable.idempotence=true   # default since Kafka 3.0 (KIP-679)
```

| acks | min.insync.replicas | RF | Guarantee |
|------|---------------------|----|-----------|
| 0    | any                 | any | None — loss on any hiccup, including client buffer drops |
| 1    | any                 | 3  | Survives nothing if the leader fails before replication |
| all  | 1 (default)         | 3  | Degrades to acks=1 whenever the ISR shrinks to the leader |
| all  | 2                   | 3  | Committed data on ≥2 replicas; still writable with one broker down |
| all  | 3                   | 3  | Any single broker outage halts writes |

The RF=3 / min.insync.replicas=2 / `acks=all` row keeps writes available through one broker failure and is the usual reference configuration.

## Unclean leader election

If every ISR replica is lost, the partition faces a choice: wait for an ISR member to return, remaining unavailable meanwhile, or elect an out-of-sync replica and discard whatever that replica is missing — **including committed records**. `unclean.leader.election.enable=false` is the default; setting it to `true` is an explicit exchange of durability for availability.

KIP-966 (**Eligible Leader Replicas**, shipped disabled by default in 4.0, on by default since 4.1, KRaft only) narrows the window. Replicas ejected from the ISR *after* it has already fallen below `min.insync.replicas` still hold every record below the HW. KIP-966 **freezes HW advancement in that state** and tracks such replicas as eligible leader replicas (ELRs), electing one of them before an unclean election is considered. This addresses the "last replica standing" case, in which brokers drop out one at a time and the final survivor — the one with the least data — would otherwise become leader.

## Leader epochs prevent divergence (KIP-101)

Before Kafka 0.11, replicas truncated their logs to their own HW on restart or leader change. Because a follower's HW trails the leader's, a restarted follower could truncate records that were in fact committed, and a sequence of failovers could leave leader and follower logs **diverged: the same offsets holding different records**.

KIP-101 replaced HW-based truncation with **leader epochs**. Each change of leadership increments a monotonic epoch number, which is stamped on every record batch written under that leadership. A recovering follower issues an `OffsetsForLeaderEpoch` request asking the leader for the end offset of the follower's last known epoch, and truncates at that point — the exact offset at which the two logs can first differ, rather than a conservative guess. A residual divergence case, arising when leadership changes twice in quick succession, was closed later by KIP-279. The epoch plays the same role in this protocol that the term plays in Raft.

### Implementation sketch (Scala)

The commit rule and the HW are both functions of the per-replica LEO map. The sketch shows the leader-side state transition on a follower fetch and the produce-time admission check; log I/O, the controller round trip and retry handling are omitted.

```scala
final case class Replica(id: Int, leo: Long, lastCaughtUpMs: Long)

final class PartitionState(
    val leaderId: Int,
    val replicas: Map[Int, Replica],
    val highWatermark: Long,
    val minInSyncReplicas: Int,
    val lagWindowMs: Long
):
  private def isrOf(state: Map[Int, Replica], nowMs: Long): Set[Int] =
    state.values.collect {
      case r if r.id == leaderId || nowMs - r.lastCaughtUpMs <= lagWindowMs => r.id
    }.toSet

  def isr(nowMs: Long): Set[Int] = isrOf(replicas, nowMs)

  /** HW is the minimum LEO across the current ISR, and never moves backwards. */
  private def recomputeHw(nowMs: Long, next: Map[Int, Replica]): Long =
    // membership is recomputed from `next`: this fetch may have re-admitted the follower
    val members = isrOf(next, nowMs)
    math.max(highWatermark, next.view.filterKeys(members).values.map(_.leo).min)

  def onFollowerFetch(id: Int, followerLeo: Long, nowMs: Long): PartitionState =
    val leaderLeo = replicas(leaderId).leo
    val caughtUp = if followerLeo >= leaderLeo then nowMs else replicas(id).lastCaughtUpMs
    val next = replicas.updated(id, Replica(id, followerLeo, caughtUp))
    PartitionState(leaderId, next, recomputeHw(nowMs, next),
                   minInSyncReplicas, lagWindowMs)

  /** acks=all admission: the floor is checked before the append, not after. */
  def admitAcksAll(nowMs: Long): Either[String, Unit] =
    val size = isr(nowMs).size
    if size >= minInSyncReplicas then Right(())
    else Left(s"NotEnoughReplicas: isr=$size < min=$minInSyncReplicas")
```

The `math.max` in `recomputeHw` encodes the monotonicity of the HW. A membership change can lower the minimum LEO — re-admitting a follower that is still behind pulls the minimum down — and the HW must never expose fewer records than consumers have already been allowed to read.

## Pitfalls

- **`acks=all` with the default `min.insync.replicas=1`** acknowledges from the leader alone once followers are ejected; a subsequent leader failure loses records that the producer saw acknowledged.
- **`min.insync.replicas` equal to the replication factor** makes any single broker outage return `NotEnoughReplicasException` for every write, halting the producer rather than degrading it.
- **Setting `min.insync.replicas` on the broker only** leaves topics created earlier with a different effective value, because the topic-level override wins; the durability check then uses a number nobody inspected.
- **Treating `acks=1` as "written to disk"**: the append lands in the page cache, so a broker host crash before flush loses the record even though the produce request succeeded.
- **Reading the leader's LEO as the consumable end of the log**: consumers stop at the HW, so a lagging ISR follower stalls consumer visibility for the entire partition while the leader keeps accepting writes.
- **Enabling `unclean.leader.election.enable` to restore availability** silently truncates committed records at the moment an out-of-sync replica is elected; the loss is not reported to producers that were already acknowledged.
- **Assuming a restart cannot diverge logs** on a cluster predating leader epochs: HW-based truncation on a follower whose HW trails the leader's is exactly the case KIP-101 was written to remove.
