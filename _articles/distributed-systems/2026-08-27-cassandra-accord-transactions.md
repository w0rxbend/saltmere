---
title: "Accord: How Cassandra Got General-Purpose Transactions Without a Leader"
date: 2026-08-27
track: distributed-systems
summary: "Accord (CEP-15) gives Cassandra strict-serializable multi-partition transactions with no elected leader: an EPaxos-style dependency protocol ordered by hybrid logical clock timestamps, a reorder buffer that delays message processing by the clock-skew bound, and fast-path electorates that shrink the fast quorum as replicas fail. Non-conflicting transactions commit in one wide-area round trip; the fast path's price is a fixed latency penalty equal to the clock-synchrony bound, and its failure mode is a Paxos-shaped second round."
reading_time: 8
tags:
- cassandra
- accord
- consensus
- leaderless
- epaxos
- transactions
sources:
- title: "CEP-15: Fast General Purpose Transactions (draft whitepaper, Apache Cassandra wiki)"
  url: https://cwiki.apache.org/confluence/download/attachments/188744725/Accord.pdf
- title: "CEP-15: General Purpose Transactions (Apache Cassandra wiki)"
  url: https://cwiki.apache.org/confluence/display/CASSANDRA/CEP-15:+General+Purpose+Transactions
- title: "Apache Cassandra 6 Accord transactions: What you need to know (Instaclustr)"
  url: https://www.instaclustr.com/blog/apache-cassandra-6-accord-transactions-what-you-need-to-know/
---

**Gist.** Leader-based transaction systems pay for their simplicity twice: the leader is a scaling bottleneck, and clients not co-located with it pay extra wide-area round trips. Accord — the protocol behind Cassandra Enhancement Proposal 15 (CEP-15), shipping with Cassandra 6 after missing the 5.0 release — removes the leader by agreeing a **hybrid logical clock timestamp** per transaction, using EPaxos-style dependency tracking to execute conflicting transactions in timestamp order, and committing in **one wide-area round trip** when a fast-path quorum accepts the proposed timestamp unchanged. The costs: every message is delayed in a **reorder buffer** by the cluster's clock-skew bound, and a transaction whose timestamp is contested falls back to a Paxos-shaped second round.

## The problem with the fast paths that came before

Leaderless state-machine replication protocols in the EPaxos family let any node coordinate any command, with a fast path that commits in a single round trip when replicas agree on ordering. Two chronic weaknesses kept them out of production systems, and the CEP-15 whitepaper names both.

First, **fast-path quorums are robust to fewer failures than the protocol tolerates overall**. With 2f + 1 replicas per shard, Tempo's fast path survives only one failure while the protocol tolerates f; Janus's survives none. Losing the fast path is more than a latency regression — every operation then consumes more messages on the remaining replicas, exactly when the system is degraded. Recent work cited in the whitepaper shows the trade-off is inherent: a protocol tolerating f failures cannot have a fast path robust to more than ⌊f/2⌋ of them with a fixed quorum.

Second, dependency-based protocols are **unstable under contention**: two transactions started simultaneously in different regions each reach their nearby replicas first, no fast-path quorum witnesses a consistent arrival order, and both fall to the slow path — EPaxos can even livelock re-proposing dependencies.

Accord's design is two techniques attacking these two weaknesses, bolted onto an otherwise recognizable timestamp-ordered consensus core.

## Technique one: fast-path electorates

Accord adopts the reconfiguration idea of Flexible Paxos. Replicas vote in a fast-path **electorate** E, a subset of the replica set; the fast-path vote threshold derives from the requirement that any recovery quorum intersect any two fast-path quorums in at least one correct replica:

**|F| = ⌈(|E| + f + 1) / 2⌉**

The trade is one-sided: removing a crashed replica from the electorate cannot reduce robustness (a quorum containing a crashed process contributes nothing) but does shrink the remaining quorums. The whitepaper's example: a shard of **r = 9** replicas tolerating **f = 4** failures can run electorates of size 9, 7, or 5, giving fast-path quorums of 7, 6, and 5 respectively. Under the maximum four tolerated failures the electorate contracts to a single simple quorum of the five correct replicas — at which point fast path and slow path use the *same* quorum, and fast-path consensus still succeeds. **The fast path never becomes unreachable under any tolerated number of failures**, and no extra per-transaction messages appear as replicas die. This is the property the paper calls *stability to failure*, and it is what leader-based systems get by re-electing a leader among survivors.

## Technique two: the timestamp reorder buffer

The contention problem is an ordering-of-arrival problem, so Accord fixes arrival order. Each process keeps a loosely synchronized clock, and the cluster measures two quantities through secondary mechanisms: **SkewMax**, the maximum instantaneous clock difference between any two nodes, and the point-to-point latency between each coordinator and replica. A replica receiving a PreAccept for transaction τ with proposed timestamp t₀ from coordinator C computes

**Deadline(t₀, C, P) = t₀ + SkewMax + max(Latency(C′, P)) − Latency(C, P)**

— the last instant at which a message with an *earlier* t₀ could still arrive from any other coordinator. The replica buffers the message in a queue ordered by t₀ and processes the queue only up to that deadline. The guarantee: for any two conflicting transactions γ and τ with t₀γ < t₀τ, γ is processed before τ at every fast-path replica, **regardless of network arrival order**. Out-of-order arrival therefore costs zero additional messages — *stability to contention*.

The buffer requires no protocol changes and cannot violate safety, because the underlying consensus is correct independent of arrival order; the deadline only shapes which path gets taken. What the fast path *assumes* is precisely that the measured bounds hold: **loosely synchronized clocks whose skew stays under SkewMax, and latencies under the measured point-to-point figures**. The whitepaper argues the price is acceptable because clock skew in datacenters with GPS-disciplined time sources combined with NTP or PTP (Network/Precision Time Protocol) can be sub-millisecond, and — the load-bearing observation — **the skew penalty is uncorrelated with wide-area latency**: each region has its own time source, so the buffer adds a fixed delay dwarfed by the cross-region round trip it saves.

## The protocol: five phases, two of them skippable in spirit

A transaction γ conflicts with τ (γ ∼ τ) if their executions do not commute. Timestamps are tuples *(time, seq, id)* — wall-clock-tracking time, a logical sequence, and the proposing process's unique identifier — so they are globally unique and totally ordered. Three timestamps matter per transaction: **t₀**, the coordinator's initial proposal; **t**, the execution timestamp being agreed; and **T**, the highest timestamp a replica has witnessed for it.

1. **PreAccept.** A coordinator C (any node near the client) proposes t₀ = (now, 0, C) to every electorate member in every shard the transaction touches. A replica that has witnessed no conflicting transaction with a higher timestamp votes t = t₀; otherwise it proposes a larger timestamp derived from the maximum it has seen. Either way it replies with its **dependencies**: the conflicting transactions it knows with lower t₀. Crucially, this set need only be a **superset of the true dependencies, filtered later during execution** — replicas do not have to witness identical histories, which is where EPaxos-style protocols livelock.
2. **Fast path or Accept.** If a fast-path quorum in every shard voted t = t₀ unanimously, that timestamp is durably decided — only t₀ can ever be recovered — and the coordinator skips straight to Commit: **one round trip**. Otherwise the coordinator takes t = the maximum proposed timestamp and runs a classic-Paxos-shaped **Accept** round with a simple majority, durably fixing which Lamport value was chosen: **two round trips, worst case**, an improvement on Caesar's three and without Tempo's serialization of commutative commands.
3. **Commit, Execute, Apply.** The coordinator disseminates the decision, waits for every dependency with a lower execution timestamp to commit and execute, computes the result, and persists it to all replicas. The properties proven in the whitepaper: **execution consistency** (all conflicting transactions apply in the same order everywhere), **execution linearizability**, and liveness — together the strict-serializable isolation Cassandra exposes through the `BEGIN TRANSACTION` CQL syntax.

### Implementation sketch (Scala)

The PreAccept decision at a replica is small enough to show whole:

```scala
final case class Ts(time: Long, seq: Int, id: Int) derives CanEqual
object Ts { given Ordering[Ts] = Ordering.by(t => (t.time, t.seq, t.id)) }

final case class TxnState(t: Ts, witnessed: Ts, preAccepted: Boolean)

class Replica(id: Int, var store: Map[TxnId, TxnState]):
  def preAccept(txn: TxnId, t0: Ts, conflicts: Set[TxnId]): (Ts, Set[TxnId]) = {
    val witnessed = conflicts.flatMap(store.get).map(_.witnessed)
    val t =
      if witnessed.forall(w => summon[Ordering[Ts]].lt(w, t0)) then t0 // fast-path vote
      else { // propose a larger timestamp: slow path likely
        val max = witnessed.max
        max.copy(seq = max.seq + 1, id = id)
      }
    store = store.updated(txn, TxnState(t, t, preAccepted = true))
    // dependencies: conflicting txns proposed before t0 — a superset suffices
    val deps = conflicts.filter(c => store.get(c).exists(s =>
      summon[Ordering[Ts]].lt(s.t, t0)))
    (t, deps)
  }
```

The coordinator's side is a fold over `(t, deps)` replies: if a fast-path quorum per shard returned `t == t0`, commit at t₀ with the union of dependencies; otherwise Accept the maximum t with a simple majority.

## When the assumptions break

Nothing in Accord's safety depends on the clocks — timestamps are logical values agreed by quorum, and a wrong SkewMax cannot corrupt the order. What breaks is the **latency profile**, in three distinct ways:

- **Clock skew exceeds SkewMax.** A coordinator with a fast clock proposes t₀ values that beat slower coordinators' proposals out of the buffer; a slow coordinator's PreAccept arrives after conflicting higher-t₀ transactions were already processed, replicas vote a larger t, and the transaction takes the Accept round. Skew converts fast-path commits into two-round-trip commits, transaction by transaction, silently.
- **Genuine conflicts.** Two conflicting transactions submitted within the buffer window are ordered correctly with no extra messages — but the later one's *execution* waits for the earlier one to commit and execute at every shard. Contention costs queueing delay in the Execute phase rather than protocol messages; a hot key serializes behind its dependency chain exactly as in any strict-serializable system.
- **The buffer itself.** Every transaction, contended or not, pays SkewMax plus the latency spread before a replica even processes its PreAccept. Overestimating the bounds taxes all traffic; underestimating them re-creates the contention instability the buffer exists to remove.

## Pitfalls

- **Treating "one round trip" as unconditional** — it holds when a fast-path quorum in every touched shard witnesses t₀ before any higher conflicting timestamp; skew or contention beyond the buffer window adds a full Accept round.
- **Sizing SkewMax from NTP's advertised accuracy rather than measured worst-case offset** — the reorder-buffer guarantee is stated over the *maximum instantaneous* clock difference, and an optimistic bound silently degrades fast-path hit rate.
- **Expecting EPaxos-style dependency agreement** — Accord replicas deliberately return dependency supersets and never need identical histories; tooling that assumes exact dependency graphs at consensus time will misread the protocol.
- **Assuming the fast path degrades like Tempo's under failure** — it does not, but only because the electorate must be actively reconfigured as replicas fail; a stale electorate containing crashed members wastes vote threshold on processes that cannot vote.
- **Reading "Cassandra 5.x feature" in older material** — CEP-15 predates Cassandra 5.0 but Accord did not ship in it; the transaction syntax lands with Cassandra 6, gated on enabling the cluster metadata service first.
