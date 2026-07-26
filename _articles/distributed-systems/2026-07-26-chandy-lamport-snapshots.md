---
title: "Chandy-Lamport snapshots: photographing a running distributed system"
date: 2026-07-26
track: distributed-systems
summary: "How the Chandy-Lamport algorithm records a consistent global state of a running distributed system using markers over FIFO channels — and why the same idea powers Flink's exactly-once checkpoints."
reading_time: 5
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

You want to answer a question about a running system: is it deadlocked? How much money is in all the accounts combined? Can I restart from here after a crash? Each answer needs a *global state* — the local state of every process plus every message currently in flight. But there is no global clock and no way to freeze everyone at once. If you poll each process at a different moment, you get a Frankenstein state: a transfer already debited from account A but not yet credited to B, so the money simply vanishes.

Chandy and Lamport's 1985 algorithm records a global state that is *consistent* — one that could have occurred — without stopping the system and without a synchronized clock. It's the theoretical bedrock under checkpointing, distributed deadlock detection, and modern stream processors like Apache Flink.

## The model: processes and FIFO channels

The system is a set of processes connected by unidirectional **channels**. Two assumptions do the heavy lifting:

| Assumption | Why it matters |
|---|---|
| Channels are **FIFO** and reliable | Messages arrive in the order sent, none lost or duplicated |
| Channels have nonzero, unbounded latency | Messages can be "in transit" — that's exactly the state we must capture |

The global state is the collection of every process's local state *plus* the contents of every channel. Capturing the channels is the whole trick: a naive snapshot that only reads process states misses the in-transit messages, and those are where inconsistencies hide.

Any process can start a snapshot at any time; multiple can start concurrently. The mechanism is a single control message: the **marker**.

## The marker rules

A marker is a special token pushed into channels. Because channels are FIFO, a marker acts as a clean divider: everything that arrives *before* the marker belongs to the pre-snapshot past on that channel; everything *after* belongs to the future. There are exactly two rules.

```python
class Process:
    def __init__(self, pid, out_channels, in_channels):
        self.pid = pid
        self.recorded = False          # have I saved my own state yet?
        self.my_state = None
        self.channel_state = {c: [] for c in in_channels}   # captured in-transit msgs
        self.recording = {c: False for c in in_channels}     # am I still logging this channel?
        self.out_channels = out_channels
        self.in_channels = in_channels

    def record_own_state_and_flood(self):
        """Marker-Sending Rule (also how an initiator starts)."""
        self.my_state = snapshot_local_state()   # e.g. account balance, deadlock flags
        self.recorded = True
        # Start logging every incoming channel for in-transit messages
        for c in self.in_channels:
            self.recording[c] = True
            self.channel_state[c] = []
        # Send a marker on every outgoing channel BEFORE any further app message
        for c in self.out_channels:
            send(c, Marker(self.pid))

    def on_marker(self, channel):
        """Marker-Receiving Rule."""
        if not self.recorded:
            # First marker I've seen: this channel was empty at snapshot time
            self.channel_state[channel] = []
            self.recording[channel] = False
            self.record_own_state_and_flood()
        else:
            # Already recorded: the marker closes this channel's log.
            # Everything logged since I recorded == messages in transit.
            self.recording[channel] = False

    def on_app_message(self, channel, msg):
        deliver(msg)
        # If I've recorded but this channel is still open, msg was in flight
        if self.recorded and self.recording[channel]:
            self.channel_state[channel].append(msg)
```

Read the two rules carefully:

- **Marker-sending rule.** A process records its own state, then — before sending any further application message — emits a marker on each outgoing channel. The initiator simply invokes this rule spontaneously.
- **Marker-receiving rule.** On the *first* marker a process ever sees, it records its own state and marks the arriving channel as empty. On *subsequent* markers, it stops recording the channel; the messages it logged between recording its state and receiving that marker *are* the channel's captured state.

The algorithm terminates once every process has received a marker on all of its incoming channels. Each process ends up holding its own local state and the state of each inbound channel; the union is the global snapshot.

## Why the cut is a consistent cut

The recorded state may never have existed at any single instant of wall-clock time — and that's fine. What matters is that it is a **consistent cut**: for every message included as *received*, its *send* is also part of the recorded past. No effect appears without its cause.

FIFO ordering is what guarantees this. Suppose process p records its state and immediately fires a marker down channel c to q. Any application message p sends on c *after* the marker sits behind it in the queue, so q processes it only after the marker — meaning q sees it as post-snapshot. Conversely, a message p sent *before* recording arrives at q ahead of the marker; if q had already recorded, q logs it as in-transit. There is no way for a message to be "received-in-the-past but sent-in-the-future." That impossible case — an orphan message — is precisely what makes a cut *inconsistent*, and the marker discipline rules it out.

So the snapshot is reachable from the actual initial state and can reach the actual current state. For a *stable property* — one that stays true once true, like a deadlock or termination — this is enough: if the property holds in the snapshot, it holds now.

## From deadlock detection to Flink's exactly-once

The classic uses are **detecting stable properties**: distributed deadlock (a wait-for cycle won't spontaneously heal) and termination detection. Van Steen and Tanenbaum present it in the coordination chapter as the canonical way to obtain a global state without global time.

The modern reincarnation is stream processing. Flink's **Asynchronous Barrier Snapshotting** (Carbone et al., 2015) is Chandy-Lamport adapted to dataflow graphs: the marker becomes a **barrier** injected at the sources and flowing with the records. When an operator has received the barrier for checkpoint *n* on all inputs (it *aligns* the barriers), it snapshots its state and forwards the barrier downstream — exactly the marker-receiving rule. Because channels between operators are FIFO, the aligned snapshot is a consistent cut of the pipeline, and Flink can persist it and, on failure, restore every operator plus the in-flight records to give **exactly-once** processing.

| Chandy-Lamport (1985) | Flink ABS |
|---|---|
| Marker | Checkpoint barrier |
| FIFO channel | FIFO stream partition |
| Record process state on first marker | Operator snapshots state when barriers align |
| Channel state = logged in-transit messages | In-flight records between operators |
| Consistent cut of a general graph | Consistent cut of the dataflow DAG |

The mechanism is 40 years old and still shipping in production the moment you enable checkpointing on a stream job.

**Try next:** Implement the `Process` class above over three nodes with FIFO queues and a bank-transfer workload, have node 0 initiate a snapshot mid-transfer, then assert that summing all process states *plus* all captured channel states always yields the conserved total — even when a transfer is in flight.
