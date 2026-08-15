---
title: "TrueTime and commit-wait: how Spanner buys external consistency with an interval"
date: 2026-07-31
track: distributed-systems
summary: "Spanner provides linearizability across a globally-distributed database without a global clock. Time is modelled as an interval rather than a point: TrueTime returns [earliest, latest] with a bounded uncertainty, and Spanner waits out that uncertainty before releasing a transaction's locks. This article explains why that single wait makes commit timestamps respect real-time order."
reading_time: 7
tags: [spanner, truetime, external-consistency, linearizability, clocks, transactions]
sources:
  - title: "Corbett et al. — Spanner: Google's Globally-Distributed Database (OSDI 2012)"
    url: "https://www.usenix.org/system/files/conference/osdi12/osdi12-final-16.pdf"
  - title: "Spanner: TrueTime and external consistency (Google Cloud Documentation)"
    url: "https://cloud.google.com/spanner/docs/true-time-external-consistency"
  - title: "Strict serializability and external consistency in Spanner (Google Cloud Blog)"
    url: "https://cloud.google.com/blog/products/databases/strict-serializability-and-external-consistency-in-spanner"
---

**Gist.** Serializability constrains the *existence* of an equivalent serial order but not *which* order, so a transaction that finished before another began may still be ordered after it. Spanner enforces **external consistency** — strict serializability, i.e. linearizability applied to whole transactions — by assigning commit timestamps from **TrueTime**, a clock that reports an interval containing the true time rather than a point, and by holding each transaction's locks until its timestamp is provably in the past. The cost is a deliberate delay on every read-write commit, proportional to the clock uncertainty.

## Time as an interval, not an instant

An ordinary clock returns a single value with no stated error. **TrueTime** does not. Its core call, `TT.now()`, returns a *TTinterval* `[earliest, latest]` that is **guaranteed to contain the absolute time at which the call was invoked**. The half-width of that interval is the uncertainty **ε** (epsilon).

ε originates in the time-distribution hierarchy. Every datacenter runs time-master servers backed by two independent reference types: masters with GPS (Global Positioning System) receivers, and **Armageddon masters** with atomic clocks. The OSDI 2012 paper states that the two have **uncorrelated failure modes**, so a GPS antenna fault or a constellation-wide anomaly does not degrade both. Machines poll masters periodically; between polls the local oscillator's drift widens ε. The paper reports ε as **a sawtooth that rises with drift and snaps back at each poll**, varying from about 1 to 7 ms over a poll interval and sitting at roughly 4 ms most of the time.

Two predicates follow from the interval:

- `TT.after(t)` — is `t` definitely in the past? True when `t < TT.now().earliest`.
- `TT.before(t)` — is `t` definitely in the future? True when `t > TT.now().latest`.

Both are **conservative**: they answer false whenever the interval straddles `t`, never claiming an ordering the clock cannot justify.

## The two rules that force real-time order

Each read-write transaction receives a single commit timestamp `s`, chosen by the coordinator leader. Two rules carry the argument.

**Start rule.** Set `s = TT.now().latest`, evaluated **after the commit request arrives**. Because `latest` upper-bounds the true time of that evaluation, `s` is **at or beyond the absolute time at which the commit began**.

**Commit-wait rule.** Do not release the transaction's locks — do not allow the commit to become observable — until `TT.after(s)` holds. The transaction becomes visible only once `s` lies in the past **under every clock's worst case**, not merely under the coordinator's best estimate.

The invariant follows directly. Suppose T1 commits before T2 begins in real time. T1 retained its locks until `TT.after(s1)` was true, so absolute time had already passed `s1` before any observer could see T1. T2 then started, and its Start rule sets `s2 = TT.now().latest`, which is at least the absolute time of T2's start, which is greater than `s1`. Therefore **`s1 < s2` whenever T1 precedes T2 in real time**, which is exactly the external-consistency requirement. Commit-wait is what pays for it: **timestamp order and real-time order can no longer disagree, because a transaction cannot be observed during the window in which its timestamp is still ambiguous**.

The expected wait is **at least 2ε** (twice the average uncertainty): the timestamp is taken at the *top* of one interval, and the wait ends when the *bottom* of a later interval passes it. Consequently ε appears directly in write latency, and also in read behaviour, since a snapshot read must wait for its read timestamp to be safely past at the serving replica. **Reducing ε reduces both.**

Read-only transactions require neither locks nor commit-wait. A read at timestamp `t_read` waits until the replica's state is current through `t_read` and then returns a consistent snapshot, permitting **lock-free consistent reads served from any sufficiently up-to-date replica**.

### Implementation sketch (Scala)

The mechanism is a bounded wait, not a consensus protocol. What follows models the interval and the two rules; two-phase commit, replication and error handling are omitted.

```scala
final case class TTInterval(earliest: Long, latest: Long) // microseconds

trait Transaction:
  def prepare(): Unit
  def assignTimestamp(s: Long): Unit
  def releaseLocks(): Unit

trait TrueTime:
  def now(): TTInterval
  def after(t: Long): Boolean  = t < now().earliest
  def before(t: Long): Boolean = t > now().latest

final class Coordinator(tt: TrueTime):

  /** Returns the commit timestamp. Locks are released only after the wait. */
  def commit(txn: Transaction): Long =
    txn.prepare() // participants durably log prepare records

    // Start rule: no earlier than the true time at which commit began.
    val s = tt.now().latest
    txn.assignTimestamp(s)

    // Commit-wait: block while any clock could still place s in the future.
    // Expected duration at least ~2*epsilon; the commit stays unobservable.
    while !tt.after(s) do Thread.onSpinWait()

    txn.releaseLocks() // the commit becomes visible exactly here
    s
```

The load-bearing detail is the **ordering of the last two statements**. Assigning `s` before the wait and releasing locks after it is what ties visibility to timestamp validity; releasing locks first would leave the transaction observable while `s` was still inside some replica's uncertainty interval, and the external-consistency argument would no longer hold.

A violation can be exhibited by simulation: run N nodes whose clocks carry independent offsets bounded by ε, record `(realStart, realCommit, assignedTs)` per transaction, and search for a pair where T1 committed before T2 started yet `ts(T1) > ts(T2)`. Such pairs appear when the wait loop is removed and disappear when it is restored.

## Pitfalls

- **Treating ε as a constant.** ε follows a sawtooth between master polls, so commit latency measured immediately after a poll understates the steady-state cost; a p99 sampled across the poll interval is the honest figure.
- **Releasing locks before `TT.after(s)` returns true.** The transaction becomes observable while its timestamp may still be in the future for another replica, and a later transaction can then receive a smaller timestamp — serializability survives, external consistency does not.
- **Using the interval's midpoint or `earliest` as the commit timestamp.** Only `latest` upper-bounds the true time of the commit request; a smaller choice can be preceded in real time by a transaction holding a larger timestamp.
- **Assuming an ordinary NTP-synchronized clock substitutes for TrueTime.** The rules depend on a *bound* on error, not on accuracy; a clock that is usually close but never states its worst case cannot make `TT.after` conservative, and the invariant fails silently rather than loudly.
- **Attributing the write-latency floor to consensus alone.** Part of read-write commit latency is commit-wait, which is independent of replication round-trips and does not shrink when replicas are moved closer together.
- **Expecting commit-wait to apply to reads.** Read-only transactions do not pay it; latency attributed to commit-wait on a read path has a different cause, typically waiting for a replica to become current through the read timestamp.
