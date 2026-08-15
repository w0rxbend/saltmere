---
title: "Chandy-Lamport snapshots: recording a running distributed system"
date: 2026-07-26
track: distributed-systems
summary: "How the Chandy-Lamport algorithm records a consistent global state of a running distributed system using markers over FIFO channels, and how the same idea underlies Flink's exactly-once checkpoints."
reading_time: 6
tags: [snapshots, chandy-lamport, global-state, consistent-cut, van-steen, flink]
sources:
  - title: "Chandy & Lamport, Distributed Snapshots: Determining Global States of Distributed Systems (ACM TOCS 1985) — PDF"
    url: "https://lamport.azurewebsites.net/pubs/chandy.pdf"
  - title: "Distributed Snapshots — ACM Transactions on Computer Systems, Vol 3, No 1 (DOI)"
    url: "https://dl.acm.org/doi/10.1145/214451.214456"
  - title: "Carbone et al., Lightweight Asynchronous Snapshots for Distributed Dataflows (arXiv 1506.08603)"
    url: "https://arxiv.org/abs/1506.08603"
  - title: "Apache Flink — Stateful Stream Processing (checkpointing, barriers)"
    url: "https://nightlies.apache.org/flink/flink-docs-master/docs/concepts/stateful-stream-processing/"
  - title: "van Steen & Tanenbaum, Distributed Systems (4th ed.) — free book"
    url: "https://www.distributed-systems.net/index.php/books/ds4/"
---

**Gist.** Questions such as "is the system deadlocked?" or "what is the sum of all account balances?" require a *global state*: the local state of every process plus every message still in transit, and no global clock exists to freeze all processes at one instant. Chandy and Lamport's 1985 algorithm records a **consistent cut** of a running system by pushing a control token — the **marker** — through first-in-first-out (FIFO) channels, without halting the system and without synchronized clocks. The cost is that the recorded state need not correspond to any instant of wall-clock time: it is a state the system *could* have passed through, which is sufficient for stable properties and for restart, but not for questions whose answers change over time.

Polling processes one at a time yields an inconsistent state instead. A transfer debited from account A and not yet credited to B is recorded with the money absent from both, because the in-flight message carrying the credit belongs to no recorded location.

## The model: processes and FIFO channels

The system is a set of processes connected by unidirectional **channels**. Two assumptions carry the correctness argument:

| Assumption | Consequence |
|---|---|
| Channels are **FIFO** and reliable | Messages arrive in send order; none lost or duplicated |
| Channel delay is arbitrary but finite | Messages can be *in transit*, and that transit content is part of the state to capture |

The global state is the collection of every process's local state **plus the contents of every channel**. Capturing the channels is the load-bearing part: a snapshot that reads process states only omits the in-transit messages, and those are where the inconsistency resides.

Any process may initiate a snapshot at any time, and several may initiate concurrently. The algorithm carries no snapshot coordinator.

## The marker rules

A marker is a distinguished token pushed into channels. Because a channel is FIFO, the marker acts as a divider on that channel: everything arriving **before** it belongs to the recorded past, everything **after** it to the future. The algorithm consists of exactly two rules.

- **Marker-sending rule.** A process records its own local state and then, **before sending any further application message**, emits a marker on each of its outgoing channels. An initiator applies this rule spontaneously.
- **Marker-receiving rule.** On the **first** marker a process ever sees, it records its own state and records the arriving channel's state as **empty**. On every **subsequent** marker, it stops recording that channel; the application messages logged on it between the moment the process recorded its state and the arrival of the marker **are** that channel's recorded state.

The algorithm terminates once every process has received a marker on each of its incoming channels. Each process then holds its own local state plus the state of each inbound channel, and the union over all processes is the global snapshot. The number of markers sent is one per channel per snapshot.

### Implementation sketch (Scala)

```scala
final case class Marker(from: Int)

final class Process(
    val pid: Int,
    inChannels: Set[Int],
    outChannels: Set[Int],
    send: (Int, Any) => Unit,
    deliver: Any => Unit,
    localState: () => Any
):
  private var recorded: Boolean = false
  private var myState: Option[Any] = None
  private val channelLog = collection.mutable.Map.from(inChannels.map(_ -> Vector.empty[Any]))
  private val recording = collection.mutable.Map.from(inChannels.map(_ -> false))

  /** Marker-sending rule; also how an initiator begins. */
  def recordAndFlood(): Unit =
    myState = Some(localState())
    recorded = true
    inChannels.foreach { c => recording(c) = true; channelLog(c) = Vector.empty }
    // Must precede any further application message on these channels.
    outChannels.foreach(c => send(c, Marker(pid)))

  /** Marker-receiving rule. */
  def onMarker(c: Int): Unit =
    if !recorded then
      recordAndFlood()
      // First marker: this channel carried nothing across the cut, so close it
      // again after the flood, which armed every inbound log.
      channelLog(c) = Vector.empty
    recording(c) = false // closes the log; what was logged is the channel state

  def onAppMessage(c: Int, msg: Any): Unit =
    deliver(msg)
    if recording(c) then channelLog(c) = channelLog(c) :+ msg
```

## Why the recorded cut is consistent

The recorded state may never have held at any single instant of wall-clock time. What holds is that it is a **consistent cut**: for every message recorded as *received*, the corresponding *send* is also in the recorded past. No effect appears without its cause.

FIFO ordering supplies this. Let process *p* record its state and immediately emit a marker on channel *c* to *q*. Any application message *p* sends on *c* afterwards queues **behind** the marker, so *q* processes it only after the marker and classifies it as post-snapshot. Conversely, a message *p* sent **before** recording reaches *q* ahead of the marker; if *q* has already recorded its own state, *q* logs that message as in-transit. The combination excludes the case of a message received in the recorded past but sent in the recorded future — the **orphan message**, which is precisely what makes a cut inconsistent.

The recorded state is therefore reachable from the actual initial state and can reach the actual current state. For a **stable property** — one that remains true once it becomes true, such as deadlock or termination — this suffices: a stable property holding in the snapshot holds now. For an unstable property the implication fails in both directions, because the snapshot corresponds to a point the system may have already left.

## From deadlock detection to Flink's exactly-once

The classical applications are **detection of stable properties**: distributed deadlock, where a wait-for cycle does not spontaneously dissolve, and termination detection. Van Steen and Tanenbaum present it as the means of obtaining a global state in the absence of global time.

The modern instance is stream processing. Flink's **Asynchronous Barrier Snapshotting (ABS)** (Carbone et al., 2015) adapts the algorithm to dataflow graphs: the marker becomes a **checkpoint barrier** injected at the sources and carried along with the records. When an operator has received the barrier for checkpoint *n* on all of its inputs — barrier **alignment** — it snapshots its state and forwards the barrier downstream, which is the marker-receiving rule. Because the stream partitions between operators are FIFO, the aligned snapshot is a consistent cut of the pipeline; Flink persists it and, on failure, restores operator state from it, which gives **exactly-once state semantics** — each record affects the persisted state once. Alignment also lets ABS avoid recording channel contents in the acyclic case: an operator that has seen the barrier on one input stalls that input until the barrier arrives on the rest, so nothing is in flight across the cut. End-to-end exactly-once additionally requires sinks that participate in the checkpoint, and Flink's unaligned checkpoints trade the stall back for recorded in-flight records.

| Chandy-Lamport (1985) | Flink ABS |
|---|---|
| Marker | Checkpoint barrier |
| FIFO channel | FIFO stream partition |
| Record process state on first marker | Operator snapshots state when barriers align |
| Channel state = logged in-transit messages | Nothing recorded under alignment; in-flight records only for unaligned checkpoints |
| Consistent cut of a general graph | Consistent cut of the dataflow DAG |

## Pitfalls

- **Non-FIFO channels break the cut.** With reordering, an application message sent after the marker can overtake it and be logged as in-transit, or a pre-marker message can arrive after the marker and be dropped from the snapshot; the result is a state where a receive has no matching send.
- **Sending an application message between recording state and emitting markers** places that message on the wrong side of the divider on every affected channel, so the receiver treats a post-snapshot message as pre-snapshot.
- **Reading process states without channel states** is the common shortcut, and it loses exactly the in-flight messages: a bank workload snapshotted this way reports a total short by the value of every transfer in transit.
- **Interpreting the snapshot as an instant.** The recorded state may never have existed at any wall-clock moment; conclusions drawn from it are sound only for stable properties or for restart, not for "the system looked like this at 12:00:00".
- **Assuming termination without inbound marker coverage.** A process with an incoming channel that never delivers a marker — because the sender crashed or the channel is unreliable — leaves its channel log open, and the snapshot never completes; the algorithm assumes reliable channels and does not itself tolerate process failure.
- **Concurrent initiators are not merged.** Independent initiations produce independent snapshots; and a process that does not distinguish markers belonging to different snapshots mixes two cuts into one recorded state.
