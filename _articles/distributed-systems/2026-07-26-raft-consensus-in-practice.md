---
title: "Raft in practice: the state machine and the two RPCs that run it"
date: 2026-07-26
track: distributed-systems
summary: "Raft looks intimidating until you separate the server's state machine (follower, candidate, leader) from the two RPCs that drive it. RequestVote picks a leader; AppendEntries replicates and commits. Everything else — terms, the log matching property, the commit rule — falls out of those two calls."
reading_time: 5
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

A leader-election lease (the pattern covered in the earlier article) tells you *who* is in charge. It says nothing about what happens to the data while leadership is changing hands, or what happens when two nodes briefly both think they're in charge. Raft is the algorithm that answers that: it keeps a *log* — not just a leader slot — consistent across a majority of servers, even through crashes, partitions, and repeated elections. Van Steen & Tanenbaum file this under replicated state machines: if every replica applies the same commands in the same order, they end up in the same state. Raft's whole job is guaranteeing that order.

## The state machine

Every server is always in exactly one of three states:

```
follower  --(election timeout, no heartbeat)-->  candidate
candidate --(wins majority of votes)-->          leader
candidate --(discovers current leader / higher term)--> follower
leader    --(discovers higher term)-->           follower
```

Followers are passive: they accept RPCs and reset an election timer (randomized, typically 150–300ms) on every valid contact. If that timer fires with no word from a leader, the follower becomes a candidate, increments a monotonically increasing **term**, votes for itself, and sends `RequestVote` to every peer. Terms are Raft's logical clock: any RPC carrying a lower term is rejected, and any server that sees a higher term immediately steps down to follower. This is what makes "at most one leader per term" enforceable — you can't out-argue a bigger term number.

## RequestVote: picking a leader without picking the wrong one

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

Two safety valves live in that pseudocode. First, "vote at most once per term" — a server persists `votedFor` and won't grant a second vote in the same term, which is why a candidate needs a strict majority, not just a plurality. Second, the up-to-date check: a candidate whose log is behind cannot win. "At least as up-to-date" means: higher `lastLogTerm` wins outright; on a tie, longer log wins. This is the **election restriction**, and it's what guarantees every future leader already holds every entry that a previous leader could have committed — no separate log-repair phase needed after an election.

## AppendEntries: replication and the log matching property

The leader is the only server that accepts client writes. It appends the command to its own log, then replicates it via the same RPC used for heartbeats (an empty `AppendEntries` sent periodically, faster than the election timeout, to suppress new elections):

```
AppendEntries(term, leaderId, prevLogIndex, prevLogTerm,
              entries[], leaderCommit) -> (term, success)

on receiving AppendEntries from leader L:
    if term < currentTerm: reject
    if log[prevLogIndex].term != prevLogTerm: reject   # consistency check
    append entries[] to log, deleting any conflicting suffix
    if leaderCommit > commitIndex:
        commitIndex = min(leaderCommit, index of last new entry)
    return success = true
```

The `prevLogIndex` / `prevLogTerm` pair is the **log matching property** in one line: if two logs contain an entry with the same index and term, every preceding entry is identical too. A follower proves it has a consistent prefix by rejecting `AppendEntries` when its entry at `prevLogIndex` doesn't match. On rejection, the leader decrements `nextIndex` for that follower and retries — walking backward until it finds a matching point, then overwriting everything after it. That's the entire mechanism for repairing a follower that missed entries or has stale garbage from a dead leader's term.

## The commit rule (and its one subtlety)

| Concept | Meaning |
|---|---|
| `nextIndex[peer]` | next log entry the leader will send that peer |
| `matchIndex[peer]` | highest entry known replicated on that peer |
| `commitIndex` | highest entry known committed on a majority |
| commit condition | entry replicated on `⌈(N+1)/2⌉` servers (a majority) |

A leader advances `commitIndex` to `N` only when a majority of `matchIndex` values are `≥ N` **and** the entry at `N` was created in the leader's *current* term. That second clause is the paper's famous fix for a subtle bug class: an entry from a previous term can sit on a majority of servers without actually being safe to commit yet, because a future leader could still overwrite it. Only once the current leader has replicated at least one entry of its own term does committing that entry also retroactively commit everything before it in the log.

Once `commitIndex` advances, every server applies the newly committed entries to its local state machine in log order — this is the moment client requests actually take effect, and it's identical logic on leader and followers, which is exactly what "replicated state machine" means.

## Where it bites in practice

- **Split brain without terms wouldn't be split brain — it'd be silent corruption.** Two candidates in the *same* term can split the vote (no majority, timeout, re-election with a new randomized timeout breaks the tie); two leaders in *different* terms is handled by the term check on every RPC, not by fencing at the network layer.
- **A minority partition can't make progress, and that's the point.** A leader stuck talking to a minority can't reach `commitIndex` majority, so it can't ack writes — it just doesn't know it's a stale leader until a client times out or it reconnects and sees a higher term.
- **Log entries are not committed just because they're replicated.** Skipping the current-term check for `commitIndex` is the most common from-scratch implementation bug (etcd/raft and hashicorp/raft both encode it explicitly rather than leaving it to inference).
- **Membership changes are a special log entry, not an out-of-band operation.** Adding or removing a server goes through the same `AppendEntries`/commit path (joint consensus or single-server changes), so cluster reconfiguration can't race ahead of the log it depends on.

**Try next:** wire up a 3-node Raft cluster with `hashicorp/raft` (or read `etcd-io/raft`'s `raft.go` state machine directly), then kill the leader mid-write and watch `nextIndex` walk backward on the new leader before it starts accepting client entries again — that's the log-repair loop from the section above, happening for real.
