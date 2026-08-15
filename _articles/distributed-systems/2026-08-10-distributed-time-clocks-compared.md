---
title: "Decoding Distributed Time: which clock to reach for when there is no global one"
date: 2026-08-10
track: distributed-systems
summary: "Physical clocks, Lamport clocks, vector clocks, hybrid logical clocks and TrueTime all answer 'in what order did these events happen?' — but they capture different properties at different costs. A side-by-side comparison and a selection rule, with the happens-before guarantees stated precisely."
reading_time: 7
tags: [logical-clocks, lamport-clocks, vector-clocks, hybrid-logical-clocks, truetime, causality, happens-before]
sources:
  - title: "Lamport — Time, Clocks, and the Ordering of Events in a Distributed System (CACM 1978)"
    url: "https://lamport.azurewebsites.net/pubs/time-clocks.pdf"
  - title: "Kulkarni, Demirbas, Madappa, Avva, Leone — Logical Physical Clocks and Consistent Snapshots in Globally Distributed Databases (2014)"
    url: "https://cse.buffalo.edu/tech-reports/2014-04.pdf"
  - title: "Corbett et al. — Spanner: Google's Globally-Distributed Database (OSDI 2012)"
    url: "https://www.usenix.org/system/files/conference/osdi12/osdi12-final-16.pdf"
  - title: "Demirbas — Use of Time in Distributed Databases (part 4): Synchronized clocks in production databases"
    url: "http://muratbuffalo.blogspot.com/2025/01/use-of-time-in-distributed-databases.html"
  - title: "Almeida, Baquero, Fonte — Interval Tree Clocks: A Logical Clock for Dynamic Systems (OPODIS 2008)"
    url: "https://gsd.di.uminho.pt/members/cbm/ps/itc2008.pdf"
---

**Gist.** A distributed system has no global clock: every node runs its own oscillator, and network synchronisation bounds the disagreement without removing it, yet snapshots, multi-version concurrency control (MVCC), conflict resolution and replicated logs all reduce to ordering events across machines. The five practical mechanisms — physical time, Lamport scalar clocks, vector clocks, hybrid logical clocks (HLC) and TrueTime — encode Lamport's happens-before partial order into comparable timestamps. Each pays a different price: metadata proportional to the number of participants, loss of wall-clock meaning, loss of concurrency detection, or added commit latency and specialised hardware.

The subject rests on one definition. Lamport's **happens-before** relation (`→`) is the smallest relation such that: (1) if `a` and `b` occur on the same process and `a` precedes `b`, then `a → b`; (2) if `a` is a send and `b` the matching receive, then `a → b`; (3) the relation is transitive. When neither `a → b` nor `b → a` holds, the events are **concurrent** (`a ∥ b`). Every mechanism below is a strategy for encoding that partial order into timestamps that can be compared mechanically.

## The five mechanisms

### Physical clocks and NTP

Events are stamped with a local reading of wall-clock time synchronised to Coordinated Universal Time (UTC) by the Network Time Protocol (NTP). The value carries meaning outside the system: it is comparable with timestamps produced by processes that never exchanged a message. The defect for ordering is that **physical clocks are not monotone with respect to `→`**. NTP slews small offsets gradually but applies a step correction for large ones, and a step can move the clock **backwards**; virtualisation jitter, leap-second smearing and inter-node skew all permit a causally later event to receive a smaller timestamp than the event that caused it. Last-writer-wins over wall-clock timestamps then selects the wrong writer, and a snapshot defined as "everything before T" silently omits data. The [clock-synchronization deep-dive](/articles/distributed-systems/2026-07-30-clock-synchronization-cristian-ntp) covers how Cristian's algorithm, the Berkeley algorithm and NTP bound — but do not eliminate — the error.

### Lamport (scalar) logical clocks

Each process keeps one integer `L`: increment on every local event and on every send; on receive, set `L = max(L, L_msg) + 1`. This establishes the forward implication and **only** the forward implication:

> `a → b ⟹ L(a) < L(b)`. The converse fails: `L(a) < L(b)` does **not** imply `a → b`, since the events may be concurrent.

A Lamport clock therefore yields a **total order consistent with causality** once ties are broken by process identifier, which is precisely what state-machine replication and total-order multicast require. It cannot *detect* concurrency: given two timestamps, nothing distinguishes causation from independence. The value also bears no relation to wall time. See [Lamport clocks and total-order multicast](/articles/distributed-systems/2026-07-30-lamport-clocks-total-order-multicast).

### Vector clocks and version vectors

Each process keeps a vector `V` holding one counter per node. It increments its own entry on each event; on receive it takes the element-wise maximum and then increments its own entry. Comparison becomes exact: `a → b` iff `V(a) < V(b)` element-wise, and if neither vector dominates the other the events are genuinely concurrent. This **detects concurrency**, which is why Dynamo-style stores attach version vectors to values so that conflicting writes surface to the application or to a conflict-free replicated data type (CRDT) for reconciliation. The cost is `O(N)` bytes per timestamp for `N` participants, plus the assumption of a stable, known set of node identifiers. See [vector clocks in ~40 lines](/articles/distributed-systems/2026-07-24-vector-clocks-in-40-lines).

### Hybrid logical clocks (HLC)

HLC (Kulkarni, Demirbas et al., 2014) combines the two scalar approaches. Each node keeps a pair `(l, c)`: `l` is the highest physical time observed, locally or in a message, and `c` is a logical counter that advances only when events tie on `l`. The paper proves HLC captures happens-before exactly as a Lamport clock does, while the invariant `l.e ≥ pt.e` together with a bounded clock skew `ε` keeps `l` within a bounded distance of physical time. The result is Lamport's causality guarantee plus a timestamp that reads approximately as wall time, at `O(1)` size. CockroachDB and MongoDB both ship it. The inability to detect concurrency is inherited from Lamport clocks, not repaired. See [Hybrid Logical Clocks](/articles/distributed-systems/2026-07-26-hybrid-logical-clocks).

Comparison is lexicographic on `(l, c)`, so the pair sorts as a `(timestamp, tie-breaker)` pair would, with a causality guarantee attached.

### Implementation sketch (Scala)

The update rules for a local event or send, and for a receive, are the load-bearing part.

```scala
final case class Ts(l: Long, c: Long) extends Ordered[Ts]:
  def compare(that: Ts): Int =
    val byL = java.lang.Long.compare(l, that.l)
    if byL != 0 then byL else java.lang.Long.compare(c, that.c)

final class Hlc(physicalMillis: () => Long):
  private var st = Ts(0L, 0L)

  def sendOrLocal(): Ts = synchronized:
    val prev = st.l
    val l = math.max(prev, physicalMillis())
    // counter advances only when physical time failed to move the pair forward
    st = Ts(l, if l == prev then st.c + 1 else 0L)
    st

  def recv(msg: Ts): Ts = synchronized:
    val prev = st
    val pt   = physicalMillis()
    val l    = math.max(math.max(prev.l, msg.l), pt)
    val c =
      if l == prev.l && l == msg.l then math.max(prev.c, msg.c) + 1
      else if l == prev.l          then prev.c + 1
      else if l == msg.l           then msg.c + 1
      else 0L                      // physical time advanced past both
    st = Ts(l, c)
    st
```

A node whose system clock steps backwards mid-run absorbs the regression into `c`: `l` never decreases, so the emitted timestamps stay monotone.

### TrueTime (Google Spanner)

Spanner takes the opposite position: make physical time trustworthy with **hardware**. GPS receivers and atomic clocks in each datacenter allow `TT.now()` to return an *interval* `[earliest, latest]` guaranteed to contain the true time, with uncertainty `ε` that the 2012 paper reports as a sawtooth of a few milliseconds, rising between synchronisations against the time masters and dropping back afterwards. The mechanism that converts bounded uncertainty into an ordering guarantee is **commit-wait**: after choosing a commit timestamp `s`, a transaction waits until `TT.now().earliest > s` — sleeping out the uncertainty, which the paper puts at roughly twice the average `ε` — before releasing its locks and making writes visible. If one transaction commits before another starts, the first therefore holds the smaller timestamp, which delivers **external consistency** (strict serializability) across datacenters. The costs are the wait latency and a GPS/atomic-clock deployment. See [TrueTime and commit-wait](/articles/distributed-systems/2026-07-31-spanner-truetime-external-consistency).

### Interval tree clocks

Vector clocks assume fixed, known membership. Interval tree clocks (Almeida, Baquero, Fonte, 2008) make the identifier space itself divisible: a node can **fork** a fresh identity locally on joining and **join** it back on departure, so metadata grows and shrinks with the cluster rather than only growing. See [Interval Tree Clocks](/articles/distributed-systems/2026-07-30-interval-tree-clocks).

## The comparison table

| | Physical / NTP | Lamport | Vector clock | HLC | TrueTime | ITC |
|---|---|---|---|---|---|---|
| Captures happens-before (`a→b ⟹ ts↑`) | No | Yes | Yes | Yes | Yes (via wait) | Yes |
| Detects concurrency (`a∥b`) | No | No | **Yes** | No | No | **Yes** |
| Wall-clock-like timestamp | Yes | No | No | **Yes** (±ε) | **Yes** (±ε) | No |
| Size per timestamp | O(1) | O(1) | **O(N)** | O(1) | O(1) | grows and shrinks with live participants |
| Needs special hardware | No (NTP) | No | No | No | **Yes** (GPS/atomic) | No |
| Handles dynamic membership | n/a | Weakly | No (fixed IDs) | Yes | Yes | **Yes** |
| Total order across all events | No | Yes (+ tie-break) | Partial only | Yes (+ tie-break) | Yes | Partial only |
| Typical use | logging, coarse LWW | SMR, total-order multicast | Dynamo conflict detection | CockroachDB, MongoDB | Spanner | dynamic P2P / CRDT metadata |

## A selection rule

- Total order for a replicated log or state machine, causality sufficient, wall-clock meaning irrelevant → **Lamport clock**, the cheapest mechanism that satisfies the requirement.
- Detection of conflicting concurrent writes (siblings, CRDT merge) over a fixed node set → **vector clock / version vector**.
- The same requirement with nodes joining and leaving continuously, where per-node metadata must not grow monotonically → **interval tree clocks**.
- Total order together with timestamps that carry wall-clock meaning (MVCC versions, snapshot reads, causal sessions) on commodity hardware → **HLC**.
- External consistency across datacenters, with GPS and atomic clocks deployable → **TrueTime with commit-wait**.
- Approximate ordering for human consumption only (log lines, dashboards), never for correctness → **plain NTP**, with no decision taken on the value.

The through-line: select the weakest mechanism that captures the property the system depends on. Concurrency detection costs `O(N)` metadata; external consistency costs hardware and commit-wait latency. Where neither is required, a scalar clock is the complete answer.

## Pitfalls

- **Comparing Lamport timestamps and concluding causality.** `L(a) < L(b)` holds for concurrent events too; treating it as evidence that `a` caused `b` misattributes independent updates.
- **Last-writer-wins over NTP timestamps.** A backwards clock step makes the later write carry the smaller timestamp, so the reconciliation discards the surviving update rather than the stale one.
- **Snapshots defined by physical time.** A node whose clock is behind commits events with timestamps below the snapshot boundary after the snapshot is taken, so the snapshot is missing writes it should contain.
- **Vector clocks with churning membership.** Every node identifier ever seen persists in the vector, so timestamp size grows with cumulative rather than current membership.
- **HLC treated as a concurrency detector.** HLC is a scalar and orders every pair of events; conflicting writes appear ordered and one is silently overwritten.
- **Removing commit-wait to reduce latency.** Making writes visible before `TT.now().earliest > s` breaks the property the wait establishes: a later transaction can then observe a commit timestamp not smaller than its own.
- **Clock skew exceeding the assumed bound.** HLC's bounded distance between `l` and physical time, and TrueTime's interval guarantee, both rest on the skew bound holding; a node outside it degrades the timestamp's physical meaning without any error being raised.
