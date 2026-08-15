---
title: "Raft in practice: the state machine and the two RPCs that run it"
date: 2026-07-26
track: distributed-systems
summary: "Raft separates into a per-server state machine (follower, candidate, leader) and two remote procedure calls that drive it. RequestVote elects a leader; AppendEntries replicates and commits. Terms, the log matching property and the commit rule follow from those two calls."
reading_time: 6
tags: [raft, consensus, replication, leader-election, fault-tolerance, van-steen]
sources:
  - title: "Ongaro & Ousterhout, In Search of an Understandable Consensus Algorithm (Extended Version)"
    url: "https://raft.github.io/raft.pdf"
  - title: "The Raft Consensus Algorithm (raft.github.io)"
    url: "https://raft.github.io/"
  - title: "HashiCorp Consul docs — Consensus Protocol (Raft)"
    url: "https://developer.hashicorp.com/consul/docs/concept/consensus"
  - title: "etcd-io/raft — Raft library for maintaining a replicated state machine"
    url: "https://github.com/etcd-io/raft"
---

**Gist.** A leader-election lease identifies who is in charge but says nothing about the data while leadership changes hands. Raft keeps a replicated *log* consistent across a majority of servers through crashes, partitions and repeated elections, by ordering every command through a single leader and committing an entry only once a majority has stored it. The cost is that **no write can be acknowledged without a round trip to a majority**, and a minority partition makes no progress at all.

Van Steen & Tanenbaum classify this under replicated state machines: if every replica applies the same commands in the same order, every replica reaches the same state. Raft's task is guaranteeing that order.

## The per-server state machine

Every server is in exactly one of three states:

```
follower  --(election timeout, no heartbeat)-->  candidate
candidate --(wins majority of votes)-->          leader
candidate --(discovers current leader / higher term)--> follower
leader    --(discovers higher term)-->           follower
```

Followers are passive: they accept remote procedure calls (RPCs) and reset a randomized election timer — the paper's evaluation uses a 150–300 ms range — on every valid contact. When the timer fires without contact from a leader, the follower becomes a candidate, increments a monotonically increasing **term**, votes for itself, and sends `RequestVote` to every peer.

Terms are Raft's logical clock. **Any RPC carrying a term lower than the receiver's `currentTerm` is rejected, and any server observing a higher term immediately reverts to follower and adopts that term.** This is the enforcement mechanism for the invariant *at most one leader per term*: a stale leader cannot argue with a larger term number, it can only step down.

## RequestVote: electing a leader that already holds the committed entries

```
RequestVote(term, candidateId, lastLogIndex, lastLogTerm) -> (term, voteGranted)

on receiving RequestVote from candidate C:
    if term < currentTerm: reject
    if term > currentTerm: currentTerm = term; become follower
    if (votedFor is null or votedFor == C)
       and C's log is at least as up-to-date as mine:
        votedFor = C; reset election timer; grant vote
    else:
        reject
```

Two safety properties are encoded there. First, **one vote per term, persisted**: `votedFor` survives a crash, so a restarted server cannot vote twice in the same term, which is what makes a strict majority — rather than a plurality — meaningful.

Second, the up-to-date comparison. "At least as up-to-date" is defined on the pair `(lastLogTerm, lastLogIndex)`: **a higher `lastLogTerm` wins outright; on equal terms, the longer log wins.** This is the **election restriction**, and it yields the property that every future leader already stores every entry a previous leader could have committed. A committed entry is on a majority; any winning candidate has a majority of votes; the two majorities intersect in at least one server, and that server refuses to vote for a candidate whose log is behind it. Consequently **there is no separate log-recovery phase after an election** — the new leader is already complete by construction.

## AppendEntries: replication and the log matching property

The leader is the only server that accepts client writes. It appends the command to its own log and replicates it with the same RPC used for heartbeats — an empty `AppendEntries` sent at an interval shorter than the election timeout, which suppresses spurious elections.

```
AppendEntries(term, leaderId, prevLogIndex, prevLogTerm,
              entries[], leaderCommit) -> (term, success)

on receiving AppendEntries from leader L:
    if term < currentTerm: reject
    if log has no entry at prevLogIndex
       or log[prevLogIndex].term != prevLogTerm: reject   # consistency check
    append entries[] to log, deleting any conflicting suffix
    if leaderCommit > commitIndex:
        commitIndex = min(leaderCommit, index of last new entry)
    return success = true
```

The `prevLogIndex` / `prevLogTerm` pair enforces the **log matching property**: if two logs contain an entry with the same index and term, all preceding entries are identical. A follower demonstrates a consistent prefix by rejecting `AppendEntries` when its entry at `prevLogIndex` does not match. **On rejection the leader decrements `nextIndex` for that follower and retries, walking backward until a matching point is found, then overwriting the entire suffix after it.** That single loop repairs both a follower that missed entries and a follower carrying uncommitted entries from a deposed leader's term.

## The commit rule and its current-term clause

| Concept | Meaning |
|---|---|
| `nextIndex[peer]` | next log entry the leader will send that peer |
| `matchIndex[peer]` | highest entry known replicated on that peer |
| `commitIndex` | highest entry known committed on a majority |
| commit condition | entry replicated on `⌊S/2⌋ + 1` of the `S` servers (a majority) |

A leader advances `commitIndex` to `N` only when a majority of `matchIndex` values are `≥ N` **and** the entry at index `N` was created in the leader's *current* term. The second clause addresses the case where **an entry from an earlier term is stored on a majority yet remains overwritable by a future leader**, because the election restriction compares terms before lengths. Once the current leader has replicated one entry of its own term, committing that entry commits everything preceding it in the log.

When `commitIndex` advances, each server applies the newly committed entries to its local state machine in log order. **The apply path is identical on leader and followers**, which is the operational content of "replicated state machine".

### Implementation sketch (Scala)

The load-bearing fragment is the leader's commit computation, not the RPC transport:

```scala
type NodeId = String

final case class Entry(term: Long, command: Array[Byte])

final class Leader(
    val currentTerm: Long,
    val log: Vector[Entry],            // 1-based indices in the paper; 0-based here
    val matchIndex: Map[NodeId, Int],  // highest entry known replicated per peer
    val clusterSize: Int
):
  private def majority: Int = clusterSize / 2 + 1

  /** Highest index N such that a majority stores N and log(N) is of the current term. */
  def commitIndex(previous: Int): Int =
    val candidates = (previous + 1 until log.size).filter: n =>
      // the current-term clause: entries from earlier terms are not committed directly
      log(n).term == currentTerm &&
        (matchIndex.values.count(_ >= n) + 1) >= majority // +1: the leader itself
    candidates.lastOption.getOrElse(previous)

  /** Follower rejected at prevLogIndex: back up one and retry from there. */
  def onAppendRejected(nextIndex: Map[NodeId, Int], peer: NodeId): Map[NodeId, Int] =
    nextIndex.updatedWith(peer)(_.map(i => math.max(0, i - 1)))

  /** Any observed higher term ends this leadership immediately. */
  def observe(term: Long): Either[Leader, Long] =
    if term > currentTerm then Right(term) else Left(this)
```

`commitIndex` returns a monotonically non-decreasing value: it never considers indices at or below `previous`, and it filters on the current term before counting replicas.

## Pitfalls

- **Committing an entry from a previous term because a majority stores it.** The entry can still be overwritten by a later leader whose log wins the up-to-date comparison on term, so an acknowledged write is lost; the current-term clause in the commit rule is what prevents it, and the paper works the scenario through in §5.4.2, "Committing entries from previous terms".
- **Treating `votedFor` and `currentTerm` as in-memory state.** A server that crashes and restarts within the same term votes a second time, so two candidates can each collect a majority and two leaders exist in one term; both fields must be persisted before the vote response is sent.
- **A fixed election timeout across the cluster.** Followers time out together, split the vote so no candidate reaches a majority, and repeat — elections fail indefinitely; the randomized 150–300 ms range exists to break that symmetry.
- **A heartbeat interval close to the election timeout.** One delayed heartbeat triggers an election and deposes a healthy leader, producing repeated leadership churn under ordinary network jitter.
- **Expecting a partitioned leader to detect its own staleness.** A leader in a minority cannot reach a majority of `matchIndex` values and therefore cannot advance `commitIndex` or acknowledge writes, but it continues to believe it is leader until it observes a higher term or a client deadline expires.
- **Performing membership changes out of band.** Adding or removing a server is a log entry replicated and committed through the same `AppendEntries` path (joint consensus, or single-server changes); applying a configuration outside the log lets the cluster's notion of "majority" change ahead of the log that depends on it.
