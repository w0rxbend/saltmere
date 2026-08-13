---
title: "Kafka Replication Internals: ISR, High Watermark, and What acks=all Really Buys You"
date: 2026-08-13
track: microservices
summary: "How Kafka commits a write: per-partition leaders, the in-sync replica set, high watermark vs log end offset, and why acks=all with the default min.insync.replicas=1 quietly degrades to acks=1. Plus leader epochs (KIP-101) and Eligible Leader Replicas (KIP-966), on by default since 4.1."
reading_time: 5
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

Most production Kafka data loss traces back to three misunderstandings: what the ISR is, what `acks` does and does not promise, and what happens when a leader dies. As of Kafka 4.3 (mid-2026) the protocol below is still the core of replication, with KIP-966's Eligible Leader Replicas as the newest safety net.

## One leader per partition

Replication happens per partition, not per topic. Each partition has one leader replica that serves all produce requests (and, by default, consumer fetches); the other replicas are followers that replicate by issuing fetch requests to the leader, much like consumers do. Every replica tracks a **log end offset (LEO)** — one past the last record in its local log.

## The ISR: Kafka's definition of "caught up"

The leader maintains the **in-sync replica set (ISR)**: itself plus every follower that has fetched up to the leader's log end within `replica.lag.time.max.ms` (default 30 s). Lag longer than that and the leader shrinks the ISR (the change is persisted through the KRaft controller); catch back up and the follower is re-added.

The rule that everything else hangs off: a record is **committed** once every member of the *current* ISR has it. Kafka promises not to lose committed records as long as at least one ISR member survives. Note the ISR is dynamic — that's the loophole behind the `acks=all` trap below.

## High watermark vs log end offset

The **high watermark (HW)** marks the committed prefix — effectively the minimum LEO across the ISR. Consumers can only read up to the HW; records between the HW and the leader's LEO exist on the leader but are uncommitted and may vanish on failover. Followers learn the HW piggybacked on fetch responses, so a follower's HW briefly trails the leader's — a small lag that caused real divergence bugs before leader epochs (below).

## What acks=0/1/all actually guarantee

- `acks=0` — fire and forget. No broker confirmation at all.
- `acks=1` — the leader appended the record to its own log (page cache, not fsync — Kafka's durability model is replication, not disk flush). Lost if the leader dies before followers fetch it.
- `acks=all` (default since 3.0, via KIP-679's idempotence-by-default) — the leader responds only after every replica *currently in the ISR* has the record.

That italicized clause is the trap. If two followers fall behind, the ISR shrinks to just the leader — and `acks=all` is then satisfied by the leader alone. Exactly when your cluster is unhealthy, `acks=all` silently degrades to `acks=1`.

## min.insync.replicas: the missing half

`min.insync.replicas` (default 1) is the fix: the leader rejects `acks=all` produce requests with `NotEnoughReplicasException` whenever the ISR is smaller than the minimum — turning silent risk into a retriable error. Durability requires setting *both* sides:

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
| all  | 3                   | 3  | Any single broker outage halts writes — usually too strict |

The RF=3 / min.isr=2 / acks=all row is the standard answer for "design a durable Kafka setup" — it tolerates one failure for writes and two for reads.

## Unclean leader election

If every ISR replica dies, Kafka must choose: wait for an ISR member to return (consistency, unavailable meanwhile) or elect an out-of-sync replica and discard whatever it's missing — including committed records. `unclean.leader.election.enable=false` is the default; flipping it to `true` is an explicit availability-over-durability trade.

KIP-966 (**Eligible Leader Replicas**, experimental in 4.0, default since 4.1 under KRaft) narrows the gap. Replicas kicked out of the ISR *after* it fell below `min.insync.replicas` still hold everything below the HW — KIP-966 freezes HW advancement in that state and tracks those replicas as ELRs, electing them before ever considering an unclean election. This fixes the "last replica standing" scenario where brokers drop out one by one and the final, possibly-corrupt survivor used to become leader by default.

## Leader epochs prevent divergence (KIP-101)

Pre-0.11, replicas truncated their logs to their own HW on restart or leader change. Because a follower's HW lags the leader's, a bounced follower could truncate committed records, and successive failovers could leave leader and follower logs *diverged* — same offsets, different records. KIP-101 replaced HW-based truncation with **leader epochs**: each leadership change increments a monotonic epoch stamped on every record batch; a recovering follower asks the leader for the end offset of its last epoch (`OffsetsForLeaderEpoch`) and truncates precisely at the divergence point. Epochs also fence requests from zombie leaders (tightened further by KIP-279). If the interviewer pushes, note the parallel: leader epochs play the same role as Raft's terms.

**Try next:** run a 3-broker Compose cluster with `min.insync.replicas=2`, produce with `acks=all`, then stop brokers one at a time and watch when `NotEnoughReplicasException` appears — and what happens to the high watermark.
