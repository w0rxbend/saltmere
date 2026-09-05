---
title: "Parallel Commits in CockroachDB: Committing a Distributed Transaction in One Round Trip"
date: 2026-08-27
track: distributed-systems
summary: CockroachDB's classic commit path paid two sequential rounds of consensus — replicate the write intents, then replicate the COMMITTED transaction record. Parallel commits collapses them into one by writing a STAGING record listing all in-flight writes and defining commit implicitly - the transaction is committed the moment every listed write achieves consensus. The saved round trip is paid for with a recovery protocol - any reader that finds a STAGING record must prove the commit status itself.
reading_time: 8
tags:
- cockroachdb
- atomic-commit
- consensus
- transactions
- two-phase-commit
- raft
sources:
- title: "Cockroach Labs blog — Parallel Commits: An Atomic Commit Protocol for Globally Distributed Transactions"
  url: https://www.cockroachlabs.com/blog/parallel-commits/
- title: CockroachDB architecture docs — Transaction Layer
  url: https://docs.cockroachlabs.com/docs/stable/architecture/transaction-layer
- title: "CockroachDB RFC 20180324 — Parallel Commit"
  url: https://github.com/cockroachdb/cockroach/blob/master/docs/RFCS/20180324_parallel_commit.md
---

**Gist.** CockroachDB's original commit path was sequential: replicate every write intent through Raft consensus, and only then replicate a transaction record with status `COMMITTED` — roughly twice the latency of a single consensus write. **Parallel commits**, introduced in CockroachDB 19.2, launches the transaction record and the final batch of intent writes simultaneously; the record carries status `STAGING` plus the list of in-flight write keys, and the transaction is defined as committed the instant all listed writes have achieved consensus, with no committed record existing anywhere yet. The client is acknowledged after one round of consensus. The cost is that commit becomes a **distributed predicate rather than a single durable fact**: any observer that encounters a `STAGING` record whose coordinator has gone silent must run a recovery procedure that evaluates the predicate itself.

## Why the old path cost two rounds

A CockroachDB transaction writes **write intents** — provisional multi-version concurrency control (MVCC) values that point at a **transaction record**, a per-transaction row keyed under the transaction's first written key. The record is the switch that makes commit atomic: flipping its status to `COMMITTED` logically commits every intent at once, because every reader that finds an intent chases the pointer to the record before deciding what the intent means.

Each write in CockroachDB — an intent or the record itself — is replicated through **Raft consensus** within its range, which costs a round trip to a quorum of replicas. In the pre-19.2 protocol the two steps could not overlap. The coordinator had to know that **every intent had durably replicated before it dared write `COMMITTED`**, because a `COMMITTED` record whose intents were lost would commit a transaction missing some of its writes — an atomicity violation, not a latency problem. So the client-perceived commit latency was one consensus round for the slowest intent batch, then a second consensus round for the record: sequential by construction. On the TPC-C benchmark, Cockroach Labs measured client-perceived latency growing at **twice the rate of inter-node round-trip time** under this protocol.

This is the same shape as classic two-phase commit (2PC): a prepare round to all participants, then a decision round, with the decision recorded only after every prepare acknowledgment.

## The STAGING record and the implicit commit condition

Parallel commits removes the ordering constraint by changing what "committed" means. When the client issues `COMMIT`, the coordinator sends, in parallel:

- the remaining intent writes of the final batch, and
- an `EndTransaction` request that moves the transaction record to status **`STAGING`** and embeds the transaction's **in-flight writes** — the key spans, with sequence numbers, of every write not yet proven replicated.

The commit condition is now disjunctive. Per the RFC, a transaction is committed if "(1) there is a transaction record with status COMMITTED, or (2) one with status STAGED and all of the intents written in the last batch of that transaction are present." Case (2) is the **implicit commit**: no single key in the system says "committed", but the distributed state proves it, and any observer with the `STAGING` record in hand can check the proof.

The coordinator waits for all of the parallel consensus writes to succeed — one round, since they proceed concurrently — and **acknowledges the client the moment the implicit commit condition holds**. Only afterwards, asynchronously and off the latency path, does it rewrite the record to `COMMITTED` (the **explicit commit**) and resolve the intents, collapsing the distributed predicate back into a single durable fact so that future readers need not evaluate it.

The load-bearing invariant is that **the implicit commit condition must be stable**: once true, nothing may make it false, because the client has already been told the transaction committed. Two rules enforce this. First, once a `STAGING` record is written, the promised writes for that epoch must not change — the coordinator cannot quietly retry a failed write elsewhere. Second, the recovery procedure below is designed so that checking the condition never falsifies it after the fact.

Parallel commits composes with **transaction pipelining**, in which intent writes throughout the transaction are replicated from leaseholders in parallel with subsequent statements rather than awaited one by one. Pipelining pushes consensus waits off the statement path; parallel commits removes the last sequential wait at `COMMIT`. Together they bring the consensus-related stalls a transaction pays toward a constant independent of statement count.

## Recovery: every reader is a potential commit arbiter

The saved round trip reappears as protocol complexity on the conflict path. A contending transaction that encounters an intent belonging to a `STAGING` transaction cannot tell from the record alone whether that transaction is committed — the record is deliberately ambiguous. If the coordinator is alive, the contender waits; the coordinator heartbeats the record, and will shortly finalize it. If the record's heartbeat has expired, the contender must run the **transaction status recovery procedure** and settle the question itself:

1. For each in-flight write listed in the `STAGING` record, issue a `QueryIntent` request with `Prevent=true` at the transaction's provisional commit timestamp.
2. Each such request checks whether the intent exists at that sequence number and timestamp — and, **as a side effect, populates the timestamp cache** on that range, guaranteeing that if the write is not there now, it can never succeed later. This is the step that makes the answer stable rather than a race: recovery does not merely observe that a write is missing, it *forecloses* it.
3. If every listed write is found, the implicit commit condition held; the recoverer rewrites the record as `COMMITTED`.
4. If any write was prevented, the condition can now never hold; the recoverer rewrites the record as `ABORTED`.

The procedure is triggered by whoever needs the answer — a conflicting reader or writer, or the garbage-collection queue sweeping abandoned records — and it is deliberately a **slow path**. Two coordinator-side behaviors keep it rare: the asynchronous upgrade to `COMMITTED` shortly after acknowledgment, and record heartbeating so that contenders can distinguish a slow coordinator from a dead one without launching recovery.

The failure mode this buys into is worth naming: a transaction can be **committed from the client's point of view while every record in the system still says `STAGING`**. If the coordinator dies in that window, correctness now depends entirely on recovery reaching the same verdict the coordinator would have — which is why the promised-write list must be exact, why it is frozen per epoch, and why the prevention step must be atomic with the existence check.

### Implementation sketch (Scala)

The essence of recovery is a fold over promised writes where the query itself closes the door it looks through:

```scala
enum TxnStatus:
  case Pending, Staging, Committed, Aborted

final case class PromisedWrite(key: String, seq: Int)

final case class StagingRecord(
    txnId: String,
    epoch: Int,
    commitTs: Long,
    inFlight: List[PromisedWrite])

trait RangeClient:
  /** Checks for the intent at (key, seq, ts). With prevent=true the
    * range also bumps its timestamp cache so that, if the intent is
    * absent, no future write by this txn at ts can ever succeed. */
  def queryIntent(w: PromisedWrite, ts: Long, prevent: Boolean): Boolean

def recover(rec: StagingRecord, ranges: RangeClient): TxnStatus = {
  val allPresent = rec.inFlight.forall { w =>
    ranges.queryIntent(w, rec.commitTs, prevent = true)
  }
  // Either verdict is now stable: a missing write has been fenced out
  // by the timestamp cache, so the implicit-commit condition is decided.
  if allPresent then TxnStatus.Committed else TxnStatus.Aborted
}
```

Twenty lines hide one real subtlety: `queryIntent` must evaluate existence and prevention atomically on the range. Checking first and fencing second would leave a window in which the coordinator's write lands between the two, letting recovery abort a transaction whose client was already acknowledged.

## Pitfalls

- **Treating `STAGING` as "not committed".** A `STAGING` record with all in-flight writes replicated *is* a committed transaction; a resolver that aborts it on sight breaks acknowledged commits. The status is only decidable after the prevention queries run.
- **Retrying a promised write after staging.** The in-flight write list is frozen for the epoch once the `STAGING` record is written; redirecting a failed write elsewhere would let recovery and the coordinator reach different verdicts.
- **Checking intent existence without fencing.** A recovery read that does not populate the timestamp cache can observe "missing" and declare abort while the write is still in flight and about to succeed — the `Prevent=true` side effect is the correctness mechanism, not an optimization.
- **Assuming the client-visible commit point equals the durable `COMMITTED` record.** Between acknowledgment and the asynchronous explicit commit, contending transactions pay either a heartbeat-wait or a full recovery pass; workloads with heavy contention on freshly committed keys surface this window as added tail latency.
