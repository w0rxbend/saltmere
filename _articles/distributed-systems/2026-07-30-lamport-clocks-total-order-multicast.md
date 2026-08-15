---
title: "Lamport Clocks and Total-Order Multicast: agreeing on order without a clock"
date: 2026-07-30
track: distributed-systems
summary: "Scalar Lamport timestamps yield a consistent happened-before ordering from message counters alone. Process-id tie-breaking plus full acknowledgement turns that partial order into total-order multicast, the delivery layer under state-machine replication."
reading_time: 6
tags: [lamport-clocks, logical-clocks, happened-before, total-order-multicast, state-machine-replication]
sources:
  - title: "Time, Clocks, and the Ordering of Events in a Distributed System (CACM 1978) — Leslie Lamport"
    url: "https://lamport.azurewebsites.net/pubs/time-clocks.pdf"
  - title: "Distributed Systems (4th ed.) — van Steen & Tanenbaum, Ch. 6 Coordination"
    url: "https://www.distributed-systems.net/index.php/books/ds4/"
  - title: "Implementing Fault-Tolerant Services Using the State Machine Approach (ACM Computing Surveys 1990) — Fred Schneider"
    url: "https://www.cs.cornell.edu/fbs/publications/smsurvey.pdf"
  - title: "Distributed Systems 6.4: Total order broadcast (lecture notes) — Martin Kleppmann"
    url: "https://www.cl.cam.ac.uk/teaching/2122/ConcDisSys/dist-sys-notes.pdf"
---

**Gist.** Physical clocks in a distributed system drift and are corrected by the Network Time Protocol (NTP), so a wall-clock comparison cannot decide whether one event caused another. Lamport's 1978 construction replaces real time with a per-process integer counter that is advanced on every local event and raised to `max(local, received) + 1` on every receipt, which guarantees that causally ordered events receive increasing timestamps. The counter buys ordering at the price of information: it can never distinguish "concurrent" from "causally ordered", and building a *total* order on top of it requires every member to acknowledge every message before any member may deliver it.

## Happened-before

Lamport defines a partial order `→` ("happened-before") over events:

1. If `a` and `b` are in the same process and `a` comes first, then `a → b`.
2. If `a` is the sending of a message and `b` is its receipt, then `a → b`.
3. Transitivity: if `a → b` and `b → c`, then `a → c`.

If neither `a → b` nor `b → a`, the events are **concurrent** — genuinely unordered. There is no correct answer to order them by, only a consistent one. Vector clocks, treated separately in this track, can *detect* this concurrency; scalar Lamport clocks cannot.

## The scalar clock

Each process keeps one integer, `C`. Three rules maintain it:

- **Before any event** (local step or a send), increment: `C := C + 1`.
- **On send**, attach the current `C` as the message timestamp `t`.
- **On receive** of a message carrying timestamp `t`: `C := max(C, t) + 1`.

The invariant, Lamport's **clock condition**, is one-directional: **if `a → b` then `C(a) < C(b)`, but `C(a) < C(b)` does not imply `a → b`**. The converse fails precisely for concurrent events, whose counters may compare in either direction depending on how many local steps each process happened to take. The receive rule is what carries causality across the network: the `max` prevents a lagging receiver from stamping an effect with a timestamp below its cause, and the `+ 1` keeps the inequality strict, so a send and its matching receipt can never share a timestamp.

## From partial order to total order

`→` leaves concurrent events unordered, yet replicas that must converge to the same state require *one* order. The standard extension is lexicographic tie-breaking: order events by the pair `(timestamp, process_id)`. **Process identifiers are unique, so no two events tie, and every process independently computes the identical sequence** without exchanging any further information. The resulting order is arbitrary where the underlying events are concurrent, and that is admissible — state-machine replication needs agreement on an order, not a canonical one.

## Total-order multicast

**Total-order (atomic) multicast** delivers messages to all group members in the same order at every member. The classical construction, adapted from the mutual-exclusion algorithm in Lamport's 1978 paper, builds it from logical clocks plus a per-process priority queue, **assuming reliable, FIFO (first-in-first-out) channels between every pair of processes**:

1. To multicast a message, a process timestamps it with its Lamport clock and sends it to **every member, itself included**. Each receiver inserts it into a local queue ordered by `(timestamp, sender_id)` and replies with a timestamped **acknowledgement** to all members.
2. A process **delivers** — that is, applies — the message at the head of its queue only when both hold: (a) the message has been acknowledged by *all* members, and (b) it is the minimum by `(timestamp, sender_id)` in the queue.

Condition (b) is the load-bearing one. Because channels are FIFO and every process acknowledges to every member, **once a message `m` has been acknowledged by all members, no message still in flight can carry a smaller `(timestamp, sender_id)`**: any message a member sends after acknowledging `m` was stamped from a clock already raised past `m`'s timestamp, and FIFO ordering means no earlier message from that member can still overtake the acknowledgement. So a fully acknowledged head of queue is stable, and each process reaches that state for `m` in the same relative position. Delivery follows `(timestamp, sender_id)` order everywhere, with **no central sequencer and no leader election**.

The cost is the message pattern. A multicast to `n` members costs `n` data messages plus `n²` acknowledgements, and delivery of any message is blocked until the slowest member's acknowledgement arrives, making end-to-end delivery latency a function of the maximum, not the average, round trip in the group.

### Implementation sketch (Scala)

The sketch below shows the clock rules and the two delivery conditions; the transport, membership and failure handling are omitted.

```scala
final case class Stamp(ts: Long, pid: Int)
given Ordering[Stamp] = Ordering.by(s => (s.ts, s.pid))

final class LamportClock:
  private var c: Long = 0
  def tick(): Long = { c += 1; c }                      // local event or send
  def merge(t: Long): Long = { c = math.max(c, t) + 1; c }  // receive

final class Node(pid: Int, members: () => Vector[Node]):   // members includes self
  private val clock = LamportClock()
  private var queue = collection.immutable.TreeMap.empty[Stamp, String]
  private var acks  = Map.empty[Stamp, Set[Int]].withDefaultValue(Set.empty)

  def multicast(msg: String): Unit =
    val s = Stamp(clock.tick(), pid)
    members().foreach(_.onData(s, msg))

  def onData(s: Stamp, msg: String): Unit = synchronized:
    clock.merge(s.ts)
    queue += s -> msg
    val a = clock.tick()
    members().foreach(_.onAck(pid, a, s))
    drain()

  def onAck(from: Int, ts: Long, s: Stamp): Unit = synchronized:
    clock.merge(ts)
    acks += s -> (acks(s) + from)
    drain()

  // Deliver only the minimum stamp, and only once every member has acked it.
  private def drain(): Unit =
    while queue.nonEmpty && acks(queue.firstKey).size == members().size do
      val (s, msg) = queue.head
      queue -= s
      applyToState(msg)

  private def applyToState(msg: String): Unit = ???
```

`TreeMap` supplies `firstKey` in logarithmic time under the `Stamp` ordering, so condition (b) is a single lookup rather than a scan.

## State-machine replication

Total-order multicast is the delivery layer beneath **state-machine replication** (Schneider 1990): replicas that start in the same state and apply the same deterministic operations in the same order remain identical. Consensus protocols such as Raft and Multi-Paxos implement an agreed, gap-free order of operations under crash and partition conditions that the FIFO-reliable formulation above assumes away. The clock-based algorithm is the same abstraction stripped of fault tolerance.

## Pitfalls

- **Reading `C(a) < C(b)` as causality.** The clock condition holds in one direction only; concluding that `a` caused `b` from timestamps alone misclassifies concurrent events, and no comparison of scalar timestamps can detect the error. Vector clocks are required to distinguish the two cases.
- **Delivering on the acknowledgement count alone.** A message that has been acknowledged by every member but is not the queue minimum must still wait: delivering it lets a lower-stamped message arrive afterwards, and the two processes that ordered them differently diverge permanently.
- **Non-FIFO channels.** Reordering acknowledgements or data on a single link breaks the stability argument for the queue head, allowing a message with a smaller `(timestamp, sender_id)` to arrive after its predecessor was delivered. The symptom is replicas that agree on the set of operations but not the sequence.
- **A silent member.** Delivery requires acknowledgements from all members, so one crashed or partitioned process stalls delivery group-wide; the algorithm has no timeout and no reconfiguration step, which is where consensus protocols begin.
- **Non-deterministic operations.** Identical order does not imply identical state if an applied operation reads local time, a random source or unsynchronised local configuration; replicas diverge with no ordering fault to find.
- **Omitting self-delivery.** A sender that applies its own message directly instead of routing it through its own queue delivers it ahead of any lower-stamped message still in flight, and diverges from every other member.
