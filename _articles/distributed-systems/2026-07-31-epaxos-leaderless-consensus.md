---
title: "EPaxos: leaderless consensus that orders only the commands that conflict"
date: 2026-07-31
track: distributed-systems
summary: "Multi-Paxos and Raft funnel every write through one leader, which caps throughput and forces wide-area clients to round-trip to a possibly-distant node. Egalitarian Paxos removes the leader: any replica commits any command, and ordering is paid for only when two commands interfere. This article covers the dependency-graph idea, the one-round-trip fast path, and the fast-quorum subtlety that a later paper had to fix."
reading_time: 6
tags: [consensus, epaxos, paxos, leaderless, quorums, replication]
sources:
  - title: "Moraru, Andersen, Kaminsky — There Is More Consensus in Egalitarian Parliaments (SOSP 2013)"
    url: "https://www.cs.cmu.edu/~dga/papers/epaxos-sosp2013.pdf"
  - title: "EPaxos project page and reference Go implementation (efficient/epaxos)"
    url: "https://github.com/efficient/epaxos"
  - title: "Ryabinin et al. — Making Democracy Work: Fixing and Simplifying Egalitarian Paxos (OPODIS 2025)"
    url: "https://arxiv.org/abs/2511.02743"
---

**Gist.** Leader-based state-machine replication agrees on a single total order of all commands, so one replica orders every write and distant clients pay a round trip to reach it. **Egalitarian Paxos (EPaxos)**, from Moraru, Andersen and Kaminsky (SOSP 2013), agrees instead on a *partial* order: each command is committed by whichever replica the client contacted, carrying the set of already-seen commands it interferes with, and non-interfering commands are never ordered against one another. The cost is that the committed structure is a **dependency graph rather than a log** — every replica must topologically sort it before execution, cycles are possible, and recovering a command whose proposer crashed is substantially harder than recovering a Raft log entry.

## Ordering less, not more

Every leader-based protocol shares one structural cost: a single replica orders all writes. Multi-Paxos and Raft elect a stable leader, and for each command that leader drives Θ(N) messages while the other replicas wait. The leader is a throughput ceiling, and in a geo-distributed cluster it is a latency trap: a client in Frankfurt talking to a leader in Oregon incurs a transatlantic round trip on every write even when a replica sits nearby.

The observation behind EPaxos is that state-machine replication over-orders. Most pairs of commands commute — `PUT x` and `PUT y` produce the same state in either order — so agreement is required only between commands that **interfere**, meaning they touch the same key and at least one of them writes.

EPaxos therefore builds no single log. Each command is proposed by whichever replica the client picked; that replica becomes the **command leader** for that one instance, and the instance carries two attributes:

- `deps`: the set of already-seen instances that interfere with this command.
- `seq`: a sequence number strictly larger than the `seq` of every instance in `deps`, used to break ties when the dependency graph contains cycles.

Committed commands form a **directed dependency graph**, not a line. At execution time each replica finds the strongly connected components of that graph, orders each component internally by `seq`, and executes components in dependency order; independent commands may execute in parallel. Because no replica is distinguished, load is spread evenly and a client always talks to its *nearest* replica.

Two commands proposed concurrently by different command leaders can each observe the other absent and each end up in the other's `deps` — the source of the cycles. The `seq` attribute exists precisely so that such a component still has a deterministic order that every replica computes identically.

## The fast path: one round trip

In the common case a replica receives command C, computes `deps` and `seq` from what it has seen locally, and sends `PreAccept` to a fast-path quorum. If every replica in that quorum returns the *same* `deps` and `seq` that were proposed — meaning no concurrent interfering command was known to any of them — the command **commits in a single round trip**. There is no leader election and no second phase.

If any reply differs, a conflicting command raced in. The command leader unions the returned dependency sets, takes the maximum returned `seq`, and runs an `Accept` phase over a classic majority to fix that order — the **slow path**, still two round trips and still no election. **Only conflicting commands pay the extra round trip**; a non-conflicting workload never leaves the fast path.

### Implementation sketch (Scala)

```scala
final case class InstanceId(replica: Int, slot: Long)
final case class Command(key: String, isWrite: Boolean)
final case class Attrs(deps: Set[InstanceId], seq: Long)
final case class PreAcceptReply(attrs: Attrs)

def interferes(a: Command, b: Command): Boolean =
  a.key == b.key && (a.isWrite || b.isWrite)

trait Transport:
  def preAccept(id: InstanceId, cmd: Command, a: Attrs, q: Set[Int]): Seq[PreAcceptReply]
  def accept(id: InstanceId, cmd: Command, a: Attrs, q: Set[Int]): Unit
  def commit(id: InstanceId, cmd: Command, a: Attrs): Unit

def localAttrs(cmd: Command, log: Map[InstanceId, (Command, Attrs)]): Attrs =
  val deps = log.collect { case (id, (other, _)) if interferes(cmd, other) => id }.toSet
  val seq  = 1L + log.view.filterKeys(deps).values.map(_._2.seq).maxOption.getOrElse(0L)
  Attrs(deps, seq)

def propose(id: InstanceId, cmd: Command, log: Map[InstanceId, (Command, Attrs)],
            fastQuorum: Set[Int], classicQuorum: Set[Int])(using t: Transport): Attrs =
  val proposed = localAttrs(cmd, log)
  val replies  = t.preAccept(id, cmd, proposed, fastQuorum)

  if replies.forall(_.attrs == proposed) then
    t.commit(id, cmd, proposed)                  // fast path: one round trip
    proposed
  else
    // union, not merge: a dependency reported by any replica must be kept
    val merged = Attrs(
      deps = replies.map(_.attrs.deps).reduce(_ union _) union proposed.deps,
      seq  = (proposed.seq +: replies.map(_.attrs.seq)).max)
    t.accept(id, cmd, merged, classicQuorum)     // slow path: two round trips
    t.commit(id, cmd, merged)
    merged
```

The sketch omits recovery, which is where the protocol's difficulty concentrates.

## The quorum subtlety

Fast-path and slow-path quorums differ in size, and this is the most error-prone part of the protocol. For **N = 2F+1** replicas the classic (slow-path) quorum is the usual majority, **F+1** — three out of five. The *basic* EPaxos fast path requires a larger quorum, **2F** (four out of five), because a replica performing recovery must be able to reconstruct what a crashed command leader could have committed on the fast path. The paper also describes a fully optimized variant that shrinks the fast quorum to **F + ⌊(F+1)/2⌋**, which is three for N = 5.

The recovery procedure that has to reconstruct such a fast-path commit is where later work found trouble. *Making Democracy Work: Fixing and Simplifying Egalitarian Paxos* (OPODIS 2025) reports correctness problems in the EPaxos recovery procedure and proposes a simplified reformulation. The practical consequence: the *idea* — leaderless, conflict-only ordering with a one-round-trip common case — is sound and has been influential, but the fault-recovery corner warrants a carefully checked implementation rather than a fresh transcription of the 2013 pseudocode.

## Where it wins

EPaxos pays off in the conditions that penalise Raft: geo-distributed clusters where client locality dominates latency, and workloads with low interference — sharded or key-partitioned access — so that most commands take the fast path. As conflicts grow denser the dependency graph thickens, the strongly connected components grow, and costs approach the slow path; at that point the operational simplicity of a single leader becomes the stronger argument.

A direct way to observe the trade-off is to start a five-replica cluster from `efficient/epaxos` and run the same YCSB-style workload twice: once with keys drawn uniformly (low interference) and once concentrated on a small hot subset (high interference), tracking the fraction of commands taking the slow path alongside tail latency.

## Pitfalls

- Treating the fast quorum as a majority: with N = 5 a three-replica `PreAccept` agreement is sufficient only under the optimized variant's conditions, and using it with basic EPaxos leaves a recovering replica unable to distinguish a fast-path commit from an uncommitted proposal.
- Computing `deps` against a partially applied local log: an instance the proposer has not yet seen is omitted from `deps`, and the resulting disagreement demotes the command to the slow path rather than causing an error — the symptom is a slow-path rate far above the measured conflict rate.
- Merging `PreAccept` replies by intersection or by taking the last reply: dependencies reported by a single replica are dropped, and two replicas can then execute interfering commands in opposite orders.
- Executing a committed command before its transitive dependencies have committed: the command appears committed while its `deps` contain instances still in progress, so execution must block until the whole reachable subgraph is committed.
- Ignoring cycles: a plain topological sort has no valid output on the dependency graph, because concurrent interfering commands can list each other; components must be ordered internally by `seq`.
- Implementing recovery from the 2013 pseudocode: the OPODIS 2025 paper reports correctness bugs in that procedure, so failures involving a crashed command leader are exactly the case least likely to have been exercised in testing.
