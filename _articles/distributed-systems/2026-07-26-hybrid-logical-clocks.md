---
title: "Hybrid Logical Clocks: timestamps that stay close to the wall clock and still capture causality"
date: 2026-07-26
track: distributed-systems
summary: "Lamport clocks capture causality but mean nothing on a wall; NTP-synced clocks mean something but can go backwards relative to causal order. HLC is the O(1)-size fix both CockroachDB and MongoDB ship in production."
reading_time: 6
tags: [hybrid-logical-clocks, logical-clocks, ntp, causality, cockroachdb, mongodb, coordination]
sources:
  - title: "Kulkarni, Demirbas, Madappa, Avva, Leone — Logical Physical Clocks and Consistent Snapshots in Globally Distributed Databases (2014)"
    url: "https://cse.buffalo.edu/tech-reports/2014-04.pdf"
  - title: "CockroachDB Docs — Transaction Layer (HLC, clock offset, uncertainty intervals)"
    url: "https://www.cockroachlabs.com/docs/stable/architecture/transaction-layer"
  - title: "CockroachDB Glossary — Hybrid Logical Clock (HLC) Timestamps"
    url: "https://www.cockroachlabs.com/glossary/distributed-db/hybrid-logical-clock-hlc-timestamps/"
  - title: "Tyulenev et al. — Implementation of Cluster-wide Logical Clock and Causal Consistency in MongoDB (SIGMOD 2019)"
    url: "https://dl.acm.org/doi/10.1145/3299869.3314049"
  - title: "Sookocheff — Hybrid Logical Clocks"
    url: "https://sookocheff.com/post/time/hybrid-logical-clocks/"
---

**Gist.** A distributed database needs event timestamps that both respect the happens-before relation and can be read as approximate wall-clock time; a Lamport clock supplies the first property and a Network Time Protocol (NTP) disciplined physical clock supplies the second, and neither supplies both. A hybrid logical clock (HLC) stores a pair `(l, c)` — a physical-time component and a tie-breaking counter — updated on every local event, send and receive, so that comparison is lexicographic and causality-respecting. The cost is that HLC inherits the Lamport clock's blindness: it totally orders events but cannot distinguish concurrency from causal order, so detecting concurrent writes still requires vector-clock-sized state.

The vector-clocks article covered the exact answer to "did A happen before B?": attach `O(N)` counters and compare vectors. That answer is precise but carries no information about *when*, in the wall-clock sense, an event occurred, and its state grows with the number of nodes in a cluster. HLC targets a narrower problem: give every event a timestamp that stays close to physical time, respects happens-before, and occupies constant space rather than a per-node vector.

## Two failing baselines

**Pure Lamport clocks.** A single integer per process, advanced on every event and on message receipt as `max(local, received) + 1`, guarantees `A → B ⟹ L(A) < L(B)`. The value has no relation to real time. `L(A) = 47` cannot be tested against a ten-second read window, and it cannot be compared with a timestamp produced by any system that never exchanged a Lamport message. The clock is causally correct and temporally meaningless.

**Pure physical clocks (NTP).** A per-node reading of the system clock is meaningful — roughly synchronised to Coordinated Universal Time (UTC) through NTP — but breaks the causality guarantee. Clock skew from NTP drift, virtualization jitter, or leap-second handling means **a later event can receive an earlier timestamp than a causally preceding event on another node**. Snapshot reads and "everything before T" queries then omit data silently, and last-writer-wins conflict resolution selects the wrong writer.

HLC (Kulkarni, Demirbas, Madappa, Avva, and Leone, 2014) combines the two. The paper proves that HLC satisfies the Lamport-clock condition — `A → B ⟹ HLC(A) < HLC(B)` — so it can be used wherever a Lamport clock is used, while remaining within a bounded distance of physical time.

## The algorithm and its invariant

Each node keeps `(l, c)`. **`l` is the highest physical time the node has observed, from its own clock or from an inbound message; `c` is a logical counter that advances only when two events tie on `l`.** `pt` denotes the node's local NTP-disciplined physical clock.

| Event | Rule |
|---|---|
| Local / send | `l' = l; l = max(l, pt); c = c+1 if l == l' else 0` |
| Receive `(l_m, c_m)` | `l' = l; l = max(l, l_m, pt)`; then: if `l`, `l'` and `l_m` all tie, `c = max(c, c_m)+1`; if only `l == l'`, `c = c+1`; if only `l == l_m`, `c = c_m+1`; otherwise `c = 0` |

The state machine has one branch per way the new `l` can have been produced. When the physical clock supplies a strictly larger value than both the previous `l` and the message's `l_m`, no tie exists and the counter resets to zero — **this reset is what keeps `c` bounded rather than growing without limit like a Lamport counter.** When the maximum comes from the previous local `l`, from the message, or from both, the counter is advanced past whichever source produced the tie, preserving strict increase along every causal edge.

The invariant proved in the paper is `l.e ≥ pt.e` for every event `e`: **the logical component never falls behind the local physical clock, and it runs ahead only by the clock skew observed in received messages.** The paper states the bound in terms of the maximum skew `ε` between any two nodes; the realised excess in a deployment is therefore whatever skew NTP leaves between the participating nodes, not an unbounded quantity.

Comparison is lexicographic on `(l, c)`. The result is a constant-size value that sorts the way a `(timestamp, tie-breaker)` pair sorts, with a happens-before guarantee attached. Every outbound message carries the pair; every inbound message is merged before the receive event is stamped.

### Implementation sketch (Scala)

```scala
final case class Hlc(l: Long, c: Long) extends Ordered[Hlc]:
  def compare(that: Hlc): Int =
    val byPhysical = java.lang.Long.compare(l, that.l)
    if byPhysical != 0 then byPhysical else java.lang.Long.compare(c, that.c)

final class HlcClock(physicalMillis: () => Long):
  private var state = Hlc(0L, 0L)

  /** Stamps a local event or an outbound message. */
  def tick(): Hlc = synchronized:
    val prev = state
    val l = math.max(prev.l, physicalMillis())
    // Counter resets whenever the physical clock supplies a strictly larger value.
    state = Hlc(l, if l == prev.l then prev.c + 1 else 0L)
    state

  /** Merges an inbound timestamp, then stamps the receive event. */
  def receive(msg: Hlc): Hlc = synchronized:
    val prev = state
    val l = math.max(math.max(prev.l, msg.l), physicalMillis())
    val c =
      if l == prev.l && l == msg.l then math.max(prev.c, msg.c) + 1
      else if l == prev.l then prev.c + 1
      else if l == msg.l then msg.c + 1
      else 0L
    state = Hlc(l, c)
    state
```

## What the construction buys a database

**CockroachDB** stamps every transaction with an HLC timestamp and uses it as the multi-version concurrency control (MVCC) version and as the transaction's read and commit timestamp. Because `l` is always at least the physical clock reading, a node can bound its uncertainty about what is concurrent with it using the configured `max_offset`, which defaults to 500 ms. That bound is the basis of the **uncertainty interval**: a read that encounters a value timestamped inside the uncertainty window pushes its own timestamp forward instead of returning a result that looks stale. CockroachDB treats skew as correctness-critical — **a node that detects skew exceeding 80% of `max_offset` against a majority of its peers terminates itself** rather than risk violating single-key linearizability.

**MongoDB** has used a cluster-wide logical clock for causal consistency since version 3.6. Every operation carries a `ClusterTime`, and the `operationTime` returned to a client after a write is supplied on subsequent reads, so a secondary can wait until it has replicated at least that far before answering. This is Lamport causality tracking anchored to physical time in the HLC manner, and it is what makes a session able to read its own writes across a replica set without pinning the session to the primary, using causally consistent sessions together with `majority` read and write concerns.

## Comparing the three

| Property | Lamport clock | Physical clock (NTP) | HLC |
|---|---|---|---|
| Captures happens-before | Yes | No | Yes |
| Close to wall-clock time | No | Yes | Yes (bounded by skew) |
| Size | O(1) | O(1) | O(1) |
| Detects concurrency exactly | No (vector clocks do) | No | No (same limit as Lamport) |
| Used by | textbook algorithms | naive last-writer-wins systems | CockroachDB, MongoDB |

The row that does not improve is concurrency detection. **HLC is exactly as weak as a Lamport clock at separating concurrent events from causally ordered ones**: the Lamport-clock condition is one-directional, so `HLC(A) < HLC(B)` carries no implication about whether `A → B`. Detecting concurrent writes, as opposed to ordering all events consistently, still requires a vector clock or a dotted version vector layered above, at `O(N)` cost. HLC's position is the middle ground: a total order plus a timestamp with physical meaning, at a size a database can attach to every row.

## Pitfalls

- **Treating an HLC value as a UTC instant.** The `l` component can exceed the local physical clock by the observed skew, so exporting it as a wall-clock reading reports times slightly in the future relative to the node's own clock.
- **Comparing `l` alone and dropping `c`.** Two causally ordered events that share a physical millisecond differ only in the counter; discarding `c` collapses them into a tie and destroys the happens-before guarantee.
- **Omitting the merge on receive.** A node that stamps inbound work with `tick()` instead of `receive(msg)` never absorbs the sender's `l`, so a message from a node ahead in physical time yields a receive timestamp below the send timestamp.
- **Assuming an ordered pair implies causal dependence.** `a < b` under HLC holds for concurrent events as well; inferring that `a` influenced `b` from the timestamps alone is unsound.
- **Running with clock discipline disabled or badly configured.** Skew is bounded only by what NTP achieves; in CockroachDB, skew past 80% of `max_offset` against a majority of peers causes the node to terminate itself, which surfaces as unexplained node loss rather than as a clock alert.
- **Persisting `c` in a narrower field than the deployment produces.** The counter resets only when the physical clock advances past every observed `l`; under a stalled or backward-stepping physical clock it keeps incrementing, and a field sized for a handful of ties overflows.
