---
title: "Byzantine Fault Tolerance and PBFT: Agreeing With Liars in the Room"
date: 2026-07-30
track: distributed-systems
summary: "Why tolerating f arbitrary, possibly-malicious replicas costs 3f+1 nodes instead of Paxos's 2f+1, and how PBFT's pre-prepare / prepare / commit phases pin down a total order even when the primary lies."
reading_time: 6
tags: [byzantine-fault-tolerance, pbft, consensus, quorum, view-change, permissioned-blockchain]
sources:
  - title: "Practical Byzantine Fault Tolerance — Castro & Liskov (OSDI 1999)"
    url: "https://css.csail.mit.edu/6.824/2014/papers/castro-practicalbft.pdf"
  - title: "Practical Byzantine Fault Tolerance — the morning paper (Adrian Colyer)"
    url: "https://blog.acolyer.org/2015/05/18/practical-byzantine-fault-tolerance/"
  - title: "Practical Byzantine Fault Tolerance — Castro & Liskov, course notes (UC Berkeley CS268)"
    url: "https://people.eecs.berkeley.edu/~istoica/classes/cs268/06/notes/BFT-osdi99x2.pdf"
  - title: "Introduction to Sawtooth PBFT (Hyperledger / LF Decentralized Trust)"
    url: "https://www.lfdecentralizedtrust.org/blog/2019/02/13/introduction-to-sawtooth-pbft"
  - title: "Distributed Systems, 4th ed. — van Steen & Tanenbaum"
    url: "https://www.distributed-systems.net/index.php/books/ds4/"
---

**Gist.** Crash-tolerant consensus protocols such as Paxos and Raft assume a failed replica goes silent, which lets `2f+1` replicas tolerate `f` failures; a replica that stays responsive and reports different values to different peers breaks that assumption. Practical Byzantine Fault Tolerance (PBFT), of Castro and Liskov (OSDI 1999), restores agreement with `3f+1` replicas and a three-phase message exchange — pre-prepare, prepare, commit — in which every replica independently reconstructs a quorum rather than trusting any single message. The cost is `O(n²)` messages per request, from the two all-to-all phases, plus a larger replica set for the same fault budget.

## Failure models

A **crash fault** is fail-stop: the node returns a correct answer or none at all. A **Byzantine fault**, named after the Byzantine Generals problem, is arbitrary: a compromised, buggy, or misconfigured replica can send different values to different peers, forge sequence numbers, replay old messages, or **equivocate** — assert one value to part of the cluster and a conflicting value to the rest. Plain majority voting no longer suffices, because the faulty replica votes too, and it does not vote consistently across recipients.

## Why 3f+1 and not 2f+1

The bound follows from a quorum-intersection argument. Progress cannot wait for all `3f+1` replicas, because `f` of them may be faulty and never answer, so a decision is taken after hearing from `N - f`. Among the `N - f` that did answer, up to `f` may be Byzantine. Two decisions taken by two different quorums must therefore overlap in at least one **honest** node, or faulty replicas could ratify two conflicting values.

Castro and Liskov state the constraint directly: "it must be possible to proceed after communicating with `n - f` replicas, since `f` replicas might be faulty and not responding. However, it is possible that the `f` replicas that did not respond are not faulty and, therefore, `f` of those that responded might be faulty."

The arithmetic: with quorum size `2f+1`, any two quorums drawn from `3f+1` nodes share at least `(2f+1) + (2f+1) - (3f+1) = f+1` nodes, and **`f+1` nodes contain at least one honest node**. That guaranteed-honest witness is what rules out equivocation. With `3f` total nodes the intersection shrinks to `f`, which could be entirely faulty. `3f+1` is therefore the minimum, and the paper states this resiliency is optimal.

| Failure model | Consensus example | Replicas for `f` faults | Quorum | Quorum intersection |
|---|---|---|---|---|
| Crash / fail-stop | Paxos, Raft | `2f+1` | `f+1` (majority) | `≥ 1` node |
| Byzantine / arbitrary | PBFT | `3f+1` | `2f+1` | `≥ f+1` nodes (`≥ 1` honest) |

Tolerating one Byzantine replica requires four replicas; tolerating two requires seven.

## The three phases

PBFT is a primary-backup scheme: one replica is the **primary** and the rest are backups. The primary of view `v` is `p = v mod N`, so a view change rotates the role deterministically. A client sends its request to the primary, which drives it through three phases.

1. **Pre-prepare.** The primary assigns the request sequence number `s` and multicasts `⟨PRE-PREPARE, v, s, D(m)⟩`, carrying the digest `D(m)`, to every backup. This proposes an ordering. A faulty primary can propose different orderings to different backups, so a pre-prepare is never acted on alone.
2. **Prepare.** A backup that accepts the pre-prepare multicasts `⟨PREPARE, v, s, D(m), i⟩` to all replicas. A replica treats the request as *prepared* once it holds the pre-prepare plus **`2f` matching prepares from distinct backups**. The prepared predicate guarantees that **no two non-faulty replicas prepare different requests for the same `s` within view `v`**.
3. **Commit.** Once prepared, a replica multicasts `⟨COMMIT, v, s, i⟩`. On collecting `2f+1` matching commits it is *committed-local*, executes the request, and replies to the client. The third phase is what carries the order across a **view change**: prepared holds only within a view, committed holds across views.

The client waits for **`f+1` matching replies from distinct replicas** before accepting the result, since at most `f` replicas lie and `f+1` agreeing replies therefore contain at least one honest reply.

Note the shape of the prepared threshold: `2f` prepares plus the primary's own pre-prepare is `2f+1` participants asserting the same order — the Byzantine quorum from the table, reconstructed independently at every replica. No single message and no single node is load-bearing.

### Implementation sketch (Scala)

The predicate below is the whole "do not trust the primary" rule: a commit is emitted only when a pre-prepare for exactly this `(view, seq, digest)` is matched by `2f` prepares from senders other than the primary.

```scala
final case class Key(view: Long, seq: Long, digest: Vector[Byte])
final case class PrePrepare(view: Long, seq: Long, digest: Vector[Byte])
final case class Prepare(view: Long, seq: Long, digest: Vector[Byte], sender: Int)

final class ReplicaLog(f: Int, n: Int):
  private var prePrepares: Map[(Long, Long), PrePrepare] = Map.empty
  private var prepares: Map[Key, Set[Int]] = Map.empty

  def primaryOf(view: Long): Int = (view % n).toInt

  /** A second pre-prepare for the same (view, seq) with a different digest is
    * equivocation evidence; it must not overwrite the first one. */
  def onPrePrepare(pp: PrePrepare): Unit =
    prePrepares.get((pp.view, pp.seq)) match
      case Some(existing) if existing.digest != pp.digest => ()
      case _ => prePrepares += ((pp.view, pp.seq) -> pp)

  def onPrepare(p: Prepare): Unit =
    val k = Key(p.view, p.seq, p.digest)
    prepares += (k -> (prepares.getOrElse(k, Set.empty) + p.sender))

  def isPrepared(view: Long, seq: Long, digest: Vector[Byte]): Boolean =
    prePrepares.get((view, seq)).exists(_.digest == digest) &&
      (prepares.getOrElse(Key(view, seq, digest), Set.empty) - primaryOf(view)).size >= 2 * f
```

With `f = 1` and four replicas, two prepares of which one carries the primary's own identifier leave `isPrepared` false, because the primary is subtracted from the sender set; the threshold is crossed only once `2f = 2` distinct backups match the recorded digest.

## View changes

A faulty primary — silent, or issuing inconsistent pre-prepares — is removed by timeout. A backup starts a timer when it accepts a request; if the timer expires before the request commits, the backup stops accepting messages for view `v` and multicasts `⟨VIEW-CHANGE, v+1, …⟩` carrying proof of what it had prepared. When the new primary, `(v+1) mod N`, collects **`2f+1` view-change messages**, it reconstructs the set of requests that might have committed and re-proposes them in the new view with a `NEW-VIEW` message. The prepared and committed certificates carried in those messages are what allow the leader to change without losing or reordering a request an honest client was already told had committed.

## Applicability

The `O(n²)` message cost makes PBFT appropriate where replicas may be adversarial or arbitrarily broken rather than merely down.

- **Permissioned blockchains.** A fixed, known validator set without mining matches PBFT's system model. Hyperledger Sawtooth's PBFT engine states the constraint as "No more than a third of the network (rounded down) can be 'out of order' or dishonest", that is, `3f+1` validators.
- **Arbitrary-failure hardware models.** Van Steen and Tanenbaum's fault-tolerance treatment frames arbitrary-failure models around components that emit plausible but wrong values instead of failing silently, which is the symptom crash-tolerant protocols cannot detect.

Crash-tolerant consensus is the right choice when replicas are trusted and only stopping is feared; PBFT applies when a replica remaining alive and lying is inside the threat model.

## Pitfalls

- **Counting the primary among the `2f` prepares.** The threshold is `2f` prepares from distinct *backups* plus the pre-prepare. Including the primary's own vote lowers the effective quorum to `2f`, and two conflicting requests can then both appear prepared.
- **Executing on the prepared predicate.** Prepared holds only within view `v`. A replica that executes without collecting `2f+1` commits can execute a request that a subsequent view change reorders or drops.
- **Accepting a client reply after one matching response.** A single reply may come from a faulty replica; correctness requires `f+1` matching replies from distinct replicas.
- **Overwriting a pre-prepare on a digest mismatch.** A primary that sends two different digests for the same `(v, s)` is equivocating; discarding the first record destroys the evidence and lets the later proposal accumulate prepares.
- **Sizing the cluster at `3f`.** Quorum intersection drops to `f` nodes, all of which may be faulty, so two quorums can ratify conflicting values.
- **Treating `f` as a count of observed failures.** `f` is a configuration parameter fixed by the replica count; exceeding it violates safety, not only liveness.
