---
title: "Session guarantees: the four promises that keep a single client's timeline straight"
date: 2026-07-30
track: distributed-systems
summary: "Data-centric consistency argues about what all clients see together. Session guarantees narrow the question: within one client's session, the store must never appear to run backwards. This article states the four guarantees from the Bayou paper, the version-vector mechanism that enforces them, and how MongoDB, DynamoDB and Cassandra deliver or drop them."
reading_time: 7
tags: [consistency, session-guarantees, causal-consistency, replication, mongodb, cassandra]
sources:
  - title: "Session Guarantees for Weakly Consistent Replicated Data (Terry, Demers, Petersen, Spreitzer, Theimer, Welch — PDIS 1994)"
    url: "https://pages.cs.wisc.edu/~remzi/Classes/739/Fall2016/Papers/bayou-sessions94.pdf"
  - title: "van Steen & Tanenbaum, Distributed Systems (4th ed.) — free PDF"
    url: "https://www.distributed-systems.net/index.php/books/ds4/"
  - title: "MongoDB Docs — Causal Consistency and Read and Write Concerns"
    url: "https://www.mongodb.com/docs/manual/core/causal-consistency-read-write-concerns/"
  - title: "Jepsen — Consistency Models"
    url: "https://jepsen.io/consistency/models"
  - title: "CASSANDRA-2494 — Quorum reads are not monotonically consistent"
    url: "https://issues.apache.org/jira/browse/CASSANDRA-2494"
---

**Gist.** In a weakly consistent replicated store, replicas hold different subsets of the write history, and a client whose successive requests land on different replicas can observe its own past disappear. The four **session guarantees** of Terry et al. (PDIS 1994) constrain only what *one* session may observe, and are enforced by carrying a **version vector** with each request and refusing to serve from a replica that does not dominate it. The cost is a liveness one: when no reachable replica dominates the required vector, the request must block, redirect, or escalate to a stronger read.

## The setting: replicas that disagree, and a client that moves

Data-centric models — linearizability, sequential consistency, causal consistency — constrain what *all* clients observe *together*. **Client-centric consistency** constrains one session in isolation and therefore never requires replicas to agree with each other. Van Steen & Tanenbaum present the family this way in the consistency-and-replication chapter: defined from the client's viewpoint rather than the data's.

Assume many servers, writes propagating lazily by gossip or anti-entropy, and no pinning of a client to a replica: a load balancer may route request 1 to replica A and request 2 to replica B. **That client mobility is the sole source of the anomalies below** — a client that never moves and never loses its replica sees a history that only grows.

The Bayou model gives each write a globally unique **write identifier (WID)** and tracks two sets per session:

- the **write-set** — WIDs of the writes this session performed;
- the **read-set** — WIDs of the writes *relevant* to this session's reads.

`DB(S,t)` denotes the set of writes that server `S` has applied at time `t`. The guarantees are constraints relating these sets to `DB`. WID sets are not transmitted literally; the paper proposes summarizing them as a **version vector**, a map from server identifier to logical clock value.

## The four guarantees

| Guarantee | Promise | Formal constraint (Bayou) |
|---|---|---|
| **Read Own Writes (RYW)** | A session observes its own past writes. | If read `R` follows write `W` in the session, then `W ∈ DB(S,t)` when `R` runs on `S`. |
| **Monotonic Reads (MR)** | A read never loses data an earlier read showed. | If `R1` precedes `R2`, then `RelevantWrites(R1) ⊆ DB(S2,t2)` when `R2` runs. |
| **Monotonic Writes (MW)** | A session's writes apply in issue order. | If `W1` precedes `W2` in the session, every server holding `W2` also holds `W1`, ordered `W1 → W2`. |
| **Writes Follow Reads (WFR)** | A write lands after the writes it was based on. | If `R1` precedes `W2`, any server holding `W2` also holds the writes `R1` read, ordered before `W2`. |

Two guarantees forbid reads from regressing (RYW, MR); two constrain write ordering (MW, WFR). The anomaly each one excludes:

- **RYW** — an address change is written, and the confirmation page reads from a replica that has not yet received the write, displaying the old value.
- **MR** — page 1 of a timeline is served by a replica holding 100 posts, page 2 by a replica holding 60, and 40 posts vanish. MR forbids a later read from reflecting *fewer* writes than an earlier one.
- **MW** — save-1 and save-2 are issued in that order; a replica applies save-2 without save-1, so the newest version omits the first edit's content.
- **WFR** — a comment is read and a reply written; the reply reaches a replica that lacks the original, so the reply is visible before what it answers. WFR preserves the read→write causal edge, which is what makes threaded discussion coherent.

Holding all four within a session gives that client a **causally ordered view of its own operations**, which is the framing MongoDB uses for its causally consistent sessions.

## The enforcement mechanism

The invariant is one line: **a replica may serve a request only if its `DB` version vector dominates the vector the session requires**, where dominance means component-wise `≥`. The session maintains two vectors, one summarizing relevant writes for its reads (serving MR and WFR) and one summarizing its own writes (serving RYW and MW), and merges observed vectors into them component-wise by `max`, so neither vector ever decreases.

**Sticky sessions are the degenerate case of this mechanism**: pinning a client to one replica makes RYW and MR nearly free, because that replica's `DB` set only grows. The guarantees become interesting exactly when stickiness is lost — failover, rebalancing, a client roaming between regions.

### Implementation sketch (Scala)

```scala
type ServerId  = String
type VV        = Map[ServerId, Long]   // server -> logical clock

def merge(a: VV, b: VV): VV =
  (a.keySet | b.keySet).view.map(s => s -> (a.getOrElse(s, 0L) max b.getOrElse(s, 0L))).toMap

def dominates(have: VV, need: VV): Boolean =
  need.forall((s, c) => have.getOrElse(s, 0L) >= c)

trait Replica:
  def version: VV
  def read(key: String): (String, VV)                       // value and its dependency vector
  def write(key: String, v: String, dependsOn: VV): VV      // returns the new write's vector

final class Session(private var readVV: VV = Map.empty, private var writeVV: VV = Map.empty):

  // RYW + MR: the serving replica must have every write this session depends on.
  def read(key: String, replicas: Seq[Replica]): Option[String] =
    val need = merge(readVV, writeVV)
    replicas.find(r => dominates(r.version, need)).map { r =>
      val (value, valueVV) = r.read(key)
      readVV = merge(readVV, valueVV)   // monotone: readVV never decreases
      value
    }   // None means no replica is caught up: block, redirect, or escalate

  // WFR (dependency on prior reads) + MW (dependency on prior writes).
  def write(key: String, value: String, replicas: Seq[Replica]): Option[VV] =
    val dep = merge(writeVV, readVV)
    replicas.find(r => dominates(r.version, dep)).map { r =>
      val wrote = r.write(key, value, dependsOn = dep)
      writeVV = merge(writeVV, wrote)
      wrote
    }
```

The `find` returning `None` is the cost the mechanism imposes: **the guarantees are safety properties, and enforcing them can only be paid for in availability or latency**, never in weaker replica agreement.

## How deployed systems map onto the model

- **Bayou.** The client holds the version vectors; any server may serve a request provided it dominates the session's token. Servers may be switched freely without violating the guarantees.
- **MongoDB causal consistency.** A causally consistent session tags reads with `afterClusterTime` and advances an `operationTime`/cluster time on each reply — the version-vector idea specialized to a hybrid logical clock. The documentation lists these four guarantees and attaches a condition: all four hold **with durability** only when reads use `readConcern: "majority"` and writes use `writeConcern: "majority"`. Weaker concerns drop guarantees without an error.
- **DynamoDB.** Eventually consistent reads provide none of the four across replicas. A strongly consistent read provides read-own-writes for that item. No cross-request session token is exposed, so monotonic reads across a session must be maintained by the caller.
- **Cassandra `LOCAL_QUORUM`.** A quorum configuration satisfying `R + W > RF` does not by itself provide monotonic reads. **CASSANDRA-2494** documents the case: a quorum read could return a newest value that the coordinator had not yet confirmed on enough replicas, so a later quorum read reaching a different replica set could return an older value. The fix made the coordinator wait for the read-repair acknowledgement before returning the value. Quorum intersection guarantees that the latest write *can* be observed, not that successive reads are monotonic.

## Pitfalls

- **Treating `R + W > RF` as monotonic reads.** Symptom: a second read returns an older value than the first under an unchanged workload. Cause: quorum intersection constrains a single read against the write set, not two reads against each other; CASSANDRA-2494 is the recorded instance.
- **Relying on sticky sessions for RYW without a fallback.** Symptom: the anomaly appears only during failover or rebalancing. Cause: stickiness supplies the guarantee implicitly, so the loss of stickiness silently removes it; no version vector is carried to detect the regression.
- **Enabling a MongoDB causally consistent session while leaving read or write concern below `majority`.** Symptom: the four guarantees hold in testing and break after a replica-set election. Cause: the documented guarantees are conditioned on `readConcern: "majority"` with `writeConcern: "majority"`; a rollback of non-majority writes is not excluded.
- **Discarding the session vectors between requests.** Symptom: read-own-writes holds within a request handler and fails across a page transition. Cause: the vectors are the entire session state; a stateless client that does not persist and resend them has no session in the model's sense.
- **Merging version vectors by replacement rather than component-wise `max`.** Symptom: a read regresses after a request served by a lagging-but-dominating replica returns a sparser vector. Cause: replacement allows a component to decrease, breaking the monotonicity the guarantees rest on.
- **Assuming a strongly consistent single-item read composes into a session guarantee.** Symptom: DynamoDB read-own-writes works per item and monotonic reads still fail across a sequence. Cause: the guarantee is scoped to one read of one item, and nothing carries the observed position forward to the next request.
