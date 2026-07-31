---
title: "Rollback Recovery: Checkpointing, the Domino Effect, and Message Logging"
date: 2026-07-31
track: distributed-systems
summary: "Saving a process's state is easy; saving a set of states you can actually restart from is not. The recovery line, the domino effect, and why message logging lets you checkpoint whenever you like — with a live CRIU demo."
reading_time: 6
tags: [fault-tolerance, checkpointing, recovery, message-logging, criu, snapshots]
sources:
  - title: "A Survey of Rollback-Recovery Protocols in Message-Passing Systems — Elnozahy, Alvisi, Wang, Johnson (ACM Computing Surveys, 2002)"
    url: "https://www.cs.utexas.edu/~lorenzo/papers/SurveyFinal.pdf"
  - title: "Message Logging: Pessimistic, Optimistic, Causal, and Optimal — Alvisi & Marzullo (IEEE TSE, 1998)"
    url: "https://www.cs.utexas.edu/~lorenzo/papers/tse.pdf"
  - title: "Distributed Systems, 4th ed. — van Steen & Tanenbaum"
    url: "https://www.distributed-systems.net/index.php/books/ds4/"
  - title: "CRIU — Checkpoint/Restore In Userspace (simple loop how-to)"
    url: "https://criu.org/Simple_loop"
  - title: "DMTCP — Distributed MultiThreaded Checkpointing (quick start)"
    url: "https://github.com/dmtcp/dmtcp/blob/main/QUICK-START.md"
---

You can snapshot one process's memory to disk in a few milliseconds. The hard part of fault tolerance isn't taking a checkpoint — it's making sure that the set of checkpoints you took across N processes describes a state the system could actually have been in. Restart from a bad set and you get a message that was *received* by one process but was never *sent* by another. That's not a crash bug; it's a violation of causality baked into your recovery images.

## The recovery line

A global state is **consistent** if, for every message recorded as received in some checkpoint, the matching send is also recorded. The **recovery line** is the most recent consistent set of checkpoints across all processes — the newest point you can safely roll back to. This is exactly the consistent global snapshot from Chandy–Lamport, viewed from the recovery side: a coordinated checkpoint protocol is essentially the marker-based snapshot algorithm run to produce a guaranteed-restartable line.

The enemy is the **orphan message**: received-but-not-sent. Its mirror image, the in-flight message (sent-but-not-yet-received), is fine — that's just a message the channel still owes you, and reliable delivery handles it.

## Uncoordinated checkpointing and the domino effect

The tempting design is to let every process checkpoint whenever it's convenient — no coordination, no blocking. The price is the **domino effect**. Suppose P1 rolls back to a checkpoint taken *before* it sent message `m` to P2. Now P2's state records receiving `m`, but P1 has no record of sending it — orphan. So P2 must also roll back to before it received `m`. But that rollback may un-send a message P2 sent to P3... and the cascade can run all the way back to the initial states, throwing away every bit of work.

To even have a chance of finding a recovery line, uncoordinated checkpointing forces you to keep *multiple* checkpoints per process and garbage-collect the ones that can never be part of any consistent line ("useless checkpoints"). **Incarnation numbers** — a version tag per run-between-failures — let you identify and discard obsolete checkpoints and messages from a process's previous life.

**Coordinated checkpointing** pays a coordination cost up front (a two-phase, snapshot-style protocol) and in return is immune to the domino effect and needs to keep only **one** permanent checkpoint per process. Simpler recovery, simpler GC. **Communication-induced checkpointing** is the middle path: mostly-autonomous local checkpoints, plus *forced* checkpoints triggered by information piggybacked on application messages, which keeps the recovery line advancing without a global barrier.

## Message logging: checkpoint whenever, replay the rest

There's a second lever. If you **log the messages** a process receives, you don't need a consistent set of checkpoints at all — a recovered process replays its logged inputs and deterministically re-computes the state it lost. That's what breaks the domino effect: rollback stops at the failed process because its peers' effects can be *reconstructed* rather than undone.

The whole thing rests on the **piecewise-deterministic (PWD) assumption**: execution is a sequence of deterministic intervals, each started by one nondeterministic event (usually a message receipt). Capture each event's **determinant** — the data needed to replay it — and you can re-run from a checkpoint to exactly the pre-failure state. A surviving process whose state depends on a determinant that was lost is an **orphan process**, and eliminating orphans is the entire game. The three protocols trade off *when* you make the determinant durable:

- **Pessimistic** — log the determinant *synchronously* to stable storage before sending any message that depends on it. No orphans, ever; recovery only touches the failed process. Highest failure-free overhead (a stable-storage write on the critical path).
- **Optimistic** — buffer determinants in volatile memory and flush asynchronously. Cheap when nothing fails, but a crash before a flush creates orphans, so recovery must track dependencies (vector-clock style) and roll them back too.
- **Causal** — piggyback not-yet-stable determinants on outgoing messages, so every determinant lives either on stable storage or in the memory of all processes causally downstream of it. Optimistic's low overhead with pessimistic's no-orphan guarantee, at the cost of fatter messages.

## Try it: the primitive under all of this

The local "freeze a consistent process state, restore it later" operation is a live demo away with **CRIU**:

```bash
# Given a running process with PID 2221:
criu dump    -t 2221 -vvv -o dump.log      # freeze + write memory/thread/fd images
criu restore -d       -vvv -o restore.log  # -d: detach and resume from the image

# For a process attached to a terminal (a shell job):
criu dump    -t 2621 --shell-job -vvvv -o dump.log
criu restore       --shell-job -vvvv -o restore.log
```

For a whole application without recompiling, **DMTCP** wraps launch and restart:

```bash
dmtcp_coordinator &            # coordination point
dmtcp_launch ./a.out &         # run under DMTCP
dmtcp_command --checkpoint     # writes ckpt_*.dmtcp + a restart script
./dmtcp_restart_script.sh      # resume
```

Scale that idea up and you get Apache Flink's exactly-once state: distributed snapshots via stream *barriers* — the Chandy–Lamport recovery line, applied to a running dataflow.

**Try next:** Write two processes that ping-pong messages and each checkpoint on a timer with no coordination. Log every send/receive with a logical clock, force one to roll back to its previous checkpoint, and print the chain of peers that must roll back with it — you'll watch the domino effect propagate. Then add a receive-log and a replay step, and confirm the cascade stops at the failed process.
