---
title: "Raft Membership Changes: Joint Consensus, Single-Server Changes, and the Bug in Between"
date: 2026-08-27
track: distributed-systems
summary: "Raft reconfigures a live cluster either through joint consensus — a two-phase transition where every decision needs majorities of both the old and new configurations — or through single-server changes, which restrict each step to adding or removing one server so the two majorities always overlap. The single-server method as published in the dissertation had a safety bug, reported by Diego Ongaro in 2015: across term boundaries, two competing configuration changes could commit through disjoint majorities. The fix is one guard — a leader may not append a configuration entry until it has committed an entry from its own term."
reading_time: 8
tags:
- raft
- consensus
- membership-change
- joint-consensus
- quorum
- reconfiguration
sources:
- title: "Ongaro & Ousterhout — In Search of an Understandable Consensus Algorithm (Extended Version), §6"
  url: https://raft.github.io/raft.pdf
- title: "Diego Ongaro — bug in single-server membership changes (raft-dev, 2015)"
  url: https://groups.google.com/g/raft-dev/c/t4xj6dJTP6E
- title: "Diego Ongaro — Consensus: Bridging Theory and Practice (PhD dissertation, Stanford, 2014), Chapter 4"
  url: https://github.com/ongardie/dissertation
---

**Gist.** A consensus cluster cannot switch atomically from configuration C-old to C-new, because there is no instant at which every server changes its notion of "majority" at once; during the switch, a majority of C-old and a majority of C-new could each elect a leader for the same term. Raft offers two defenses: **joint consensus**, a two-phase protocol in which every decision temporarily requires majorities of *both* configurations, and the simpler **single-server change**, which permits only one addition or removal at a time so that any majority of C-old and any majority of C-new must share a server. The single-server method trades protocol machinery for a subtler proof obligation — and the version published in Ongaro's dissertation violated it: until a 2015 fix, competing changes across term boundaries could commit through **disjoint majorities**.

## Why reconfiguration is dangerous at all

Raft's safety argument rests on one overlap property: any two majorities of the cluster intersect, so a candidate that wins an election has contacted at least one server holding every committed entry. Reconfiguration threatens exactly this property. If a 3-server cluster {A, B, C} is changed directly to a 5-server cluster {A, B, C, D, E}, there is a window in which A and B still believe the cluster has 3 servers (majority: 2) while C, D, E believe it has 5 (majority: 3). **{A, B} and {C, D, E} are disjoint**, and each is a valid majority under its own configuration. Two leaders could be elected for the same term, and two different values could be committed at the same log index — the precise failure consensus exists to rule out.

One further rule shapes everything below: in Raft, a configuration is itself a log entry, and **a server uses the newest configuration in its log as soon as the entry is stored, without waiting for it to commit** (Ongaro & Ousterhout, §6). Waiting for commitment would be circular — committing the entry requires knowing which majority to count. The cost of this rule is that an uncommitted, later-overwritten configuration entry still governs a server's voting and counting while it sits in the log; both bugs and both defenses in this article follow from that.

## Joint consensus: the two-phase transition

The Raft paper's answer is to route the cluster through an explicit intermediate configuration, written **C-old,new**, that combines both memberships. While C-old,new governs:

- A log entry is committed only when it is stored on **a majority of C-old and, separately, a majority of C-new**.
- A candidate wins an election only with **votes from majorities of both** configurations.

The transition has two phases. The leader first appends the C-old,new entry and replicates it; from the moment a server stores it, that server applies the joint rules. Once C-old,new is committed (under joint rules), the leader appends the **C-new** entry, and once a server stores *that*, it uses C-new alone. When C-new commits, servers not in it can be shut down.

The safety argument is a case analysis on where a leader can be elected during the transition. Before C-old,new is committed, any elected leader needs a C-old majority, and any two C-old majorities intersect. After C-new is appended, any elected leader needs a C-new majority. In between, a leader needs both, so it overlaps every possible earlier and later quorum. **C-old alone and C-new alone can never make unilateral decisions in the same term**, because no reachable pair of quorums is disjoint. Availability is preserved throughout — the cluster keeps serving requests during both phases — at the price of two configuration entries, a compound quorum check on every commit and every election, and a leader that may be managing (and even committing entries through) a configuration it is not itself a member of.

## Single-server changes: overlap by arithmetic

Chapter 4 of Ongaro's dissertation observes that most of that machinery exists to handle *arbitrary* configuration jumps. Restrict each change to **adding or removing exactly one server**, and the overlap property holds by counting: for any n, a majority of an n-server set and a majority of the corresponding (n±1)-server set differ in at most one member and must share at least one server. A 3→4 change needs 2-of-3 and 3-of-4; any such pair intersects. No joint phase, no compound quorums — the leader appends the new configuration entry, servers adopt it on storage as usual, and the entry commits under the *new* configuration's majority. Multi-server changes become a sequence of single steps.

This is the method most production implementations adopted, and the arithmetic argument is correct — for two *adjacent* configurations. What it does not cover on its own is three or more configurations in flight at once.

## The bug: disjoint majorities across term boundaries

In July 2015, Diego Ongaro reported on the raft-dev list that **the dissertation's single-server algorithm is unsafe as published**. The failure needs two competing changes started by different leaders in different terms, each beginning from C-old. Because a deposed leader's uncommitted configuration entry still sits in some logs — and still governs those servers — the cluster can hold three configurations at once, and the two *new* ones were never checked against each other. The scenario from the report, on a 4-server cluster {S1..S4}:

1. Leader **S1** appends configuration **D** = C-old + S5 (adding a server) in term 1, replicates it only to itself and S5, then stalls.
2. **S2** is elected leader in term 2 — it can win, since {S2, S3, S4} is a majority of C-old — and appends configuration **E** = C-old − S4 (removing a server). E reaches a majority of *its* quorum and **commits**.
3. **S1** regains leadership in term 3. Its log still ends with D, which is one entry ahead by the log-comparison vote rule among the servers it contacts, and D's quorum (3 of {S1..S5}) is satisfiable **without any server that stored E**.
4. S1 replicates D everywhere, **overwriting the committed entry E**.

A committed configuration was lost: the quorum that committed E and the quorum that elected and sustained S1 were **disjoint**. Each pairwise step obeyed the single-server rule; the interleaving of D and E — two changes that never co-existed in one leader's log — did not. Joint consensus is immune, because its intermediate entry forces every quorum during a transition to span both configurations.

## The fix: commit a no-op first

The repair Ongaro proposed is a single guard: **a leader may not append a new configuration entry until it has committed an entry from its current term.** Since Raft leaders already append a no-op entry on election (the standard remedy for the "leader cannot count replicas of old-term entries" problem in §5.4.2 of the paper), the rule reduces to: wait for the election no-op to commit, then accept configuration changes.

The guard works because committing a current-term entry settles the log prefix. In the scenario above, S1 in term 3 would first have to commit a term-3 no-op — which requires a quorum under D, and replicating S1's log to that quorum either succeeds in making D durable *before* any competing change, or fails because a majority has moved on to term 2's history, in which case S1's D is itself overwritten. **At most one uncommitted configuration change can be in flight per committed prefix**, restoring the invariant the arithmetic argument silently assumed. The cost is latency, not machinery: one extra committed entry per leadership change before reconfiguration may begin.

### Implementation sketch (Scala)

The load-bearing pieces are the quorum predicate — plain for a simple configuration, compound for a joint one — and the leader-side guard.

```scala
enum Config {
  case Simple(voters: Set[NodeId])
  case Joint(oldC: Set[NodeId], newC: Set[NodeId]) // C-old,new

  def quorum(acks: Set[NodeId]): Boolean = this match {
    case Simple(v)      => acks.intersect(v).size * 2 > v.size
    case Joint(o, n)    => // majorities of BOTH configurations
      acks.intersect(o).size * 2 > o.size &&
      acks.intersect(n).size * 2 > n.size
  }
}

final case class Leader(term: Long, commitIndex: Long, log: Vector[Entry]) {

  // The 2015 fix: refuse reconfiguration until an entry
  // from THIS term has committed (the election no-op suffices).
  private def committedInCurrentTerm: Boolean =
    commitIndex > 0 && log(commitIndex.toInt - 1).term == term

  def proposeConfigChange(c: Config): Either[String, Entry] =
    if (!committedInCurrentTerm)
      Left("no entry from current term committed yet")
    else if (log.drop(commitIndex.toInt).exists(_.isConfig))
      Left("a configuration change is already in flight")
    else Right(Entry(term, config = Some(c)))
}
```

Servers apply `Config` from the newest configuration entry *stored* in the log, committed or not; the guard lives only on the propose path.

## Pitfalls

- **Waiting for a configuration entry to commit before using it** deadlocks or breaks the safety argument — commitment is defined *by* the configuration, so servers must switch on storage.
- **Allowing a configuration change before an entry of the leader's own term has committed** reintroduces the 2015 disjoint-majorities bug; the election no-op must be committed first.
- **Permitting two uncommitted configuration entries in one log** voids the single-server overlap argument, which only relates adjacent configurations.
- **Adding a fresh, empty-logged server as an immediate voter** can stall commitment while it catches up; the dissertation adds servers as non-voting learners until their logs are close to current.
- **Removing the leader's own server under joint consensus** requires the leader to keep replicating (and step down only after C-new commits), or the transition never completes.
