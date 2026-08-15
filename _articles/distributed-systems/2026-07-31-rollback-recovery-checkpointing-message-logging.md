---
title: "Rollback Recovery: Checkpointing, the Domino Effect, and Message Logging"
date: 2026-07-31
track: distributed-systems
summary: "Saving one process's state is easy; saving a set of states the system can restart from is not. The recovery line, the domino effect, and why message logging decouples checkpoint timing from consistency — with a CRIU demonstration."
reading_time: 7
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

**Gist.** Snapshotting a single process's memory is a local operation, but a set of independently taken snapshots across N processes need not describe any state the system ever occupied: one process's image may record receiving a message no other image records sending. Rollback-recovery protocols restore this consistency either by coordinating checkpoints or by logging the nondeterministic events needed to replay execution deterministically. Coordination costs a global barrier and constrains when checkpoints may be taken; logging costs stable-storage writes or larger messages on the failure-free path.

## The recovery line

A global state is **consistent** if, for every message recorded as received in some checkpoint, the matching send is also recorded. The **recovery line** is the most recent consistent set of checkpoints across all processes — the newest point the system can be rolled back to. The consistency condition is the same one a distributed-snapshot algorithm establishes, viewed from the recovery side rather than the observation side: coordinated checkpoint protocols and marker-based snapshot algorithms both exist to produce a cut with no orphan message across it.

The failure mode is the **orphan message**: recorded as received, not recorded as sent. Its mirror image, the in-flight message (sent but not yet received), does not violate consistency — it is a message the channel still owes the receiver, and reliable delivery covers it. The asymmetry is the whole reason the recovery line exists as a concept: missing sends are unrecoverable from the images alone, missing receives are not.

## Uncoordinated checkpointing and the domino effect

Letting every process checkpoint whenever local conditions favour it removes coordination and blocking entirely. The price is the **domino effect**. Suppose P1 rolls back to a checkpoint taken *before* it sent message `m` to P2. P2's checkpoint records the receipt of `m` while P1 has no record of the send, so `m` is an orphan and P2 must also roll back to a state preceding that receipt. That rollback may un-send a message P2 had sent to P3, and the cascade propagates. In the worst case it reaches the processes' initial states, discarding all computed work despite every process holding recent checkpoints.

Because no single checkpoint per process is guaranteed to lie on a consistent line, uncoordinated checkpointing requires **multiple retained checkpoints per process** plus garbage collection of those that can never belong to any consistent line — the *useless checkpoints*. Recovery protocols that must tell a process's current life apart from a previous one tag each run-between-failures with an **incarnation number**, so state and messages belonging to a superseded incarnation can be identified and discarded; the survey introduces the device in the context of optimistic message logging.

**Coordinated checkpointing** pays the coordination cost up front with a two-phase, snapshot-style protocol. In return it is immune to the domino effect and needs to retain only **one permanent checkpoint per process**, which collapses both recovery and garbage collection to a single case. **Communication-induced checkpointing** occupies the middle: processes take mostly autonomous local checkpoints, but information piggybacked on application messages can *force* an additional checkpoint, advancing the recovery line without a global barrier.

## Message logging: checkpoint freely, replay the rest

Logging the messages a process receives removes the requirement that checkpoints be mutually consistent at all. A recovered process restarts from any checkpoint and replays its logged inputs, recomputing the state it lost. Rollback then stops at the failed process, because the effects its peers observed are **reconstructed rather than undone**.

The correctness of replay rests on the **piecewise-deterministic (PWD) assumption**: execution is a sequence of deterministic intervals, each begun by a single nondeterministic event, typically a message receipt. For each such event the protocol captures a **determinant** — the data needed to replay that event, identifying which message was delivered and where in the receive order it fell. Given the checkpoint plus every determinant after it, replay reproduces the pre-failure state exactly. A surviving process whose state depends on a determinant that did not survive the crash is an **orphan process**; eliminating orphans is what the three protocol families differ over, and they differ only in *when* a determinant is made durable.

- **Pessimistic logging** writes the determinant **synchronously** to stable storage before the process sends any message that depends on it. No orphan can exist, and recovery touches only the failed process. The cost is a stable-storage write on the critical path of the failure-free execution.
- **Optimistic logging** buffers determinants in volatile memory and flushes them asynchronously. Failure-free overhead is low, but a crash before a flush loses determinants and creates orphans, so recovery must track inter-process dependencies (vector-clock style) and roll orphans back as well.
- **Causal logging** piggybacks not-yet-stable determinants on outgoing messages, maintaining the invariant that **every determinant resides either on stable storage or in the volatile memory of every process causally downstream of it**. That yields the no-orphan property without a synchronous write, at the cost of larger messages.

### Implementation sketch (Scala)

The load-bearing structure is the determinant and the check that no message leaves a process before the determinants it depends on are durable — the pessimistic rule.

```scala
final case class Determinant(
    source: String,      // sending process
    seq: Long,           // sender sequence number
    receiver: String,
    deliveryIndex: Long  // position in the receiver's delivery order
)

trait StableStorage:
  def append(d: Determinant): Unit   // returns only after the write is durable

final class PessimisticReceiver(id: String, store: StableStorage):
  private var delivered = 0L
  private var pending: List[Determinant] = Nil

  /** Records the delivery order before the application observes the message. */
  def deliver(source: String, seq: Long, payload: Array[Byte]): Array[Byte] =
    val d = Determinant(source, seq, id, delivered)
    delivered += 1
    pending = d :: pending
    payload

  /** Pessimistic invariant: nothing dependent on a volatile determinant escapes. */
  def send(to: String, payload: Array[Byte])(transmit: (String, Array[Byte]) => Unit): Unit =
    pending.reverse.foreach(store.append)
    pending = Nil
    transmit(to, payload)
```

Replay after a crash re-executes from the checkpoint, feeding messages back in `deliveryIndex` order; any receive whose determinant is absent from stable storage marks the boundary beyond which the state cannot be reconstructed.

## The local primitive

The single-process operation underneath all of this — freeze a process state, restore it later — is directly observable with **CRIU** (Checkpoint/Restore In Userspace):

```bash
# $PID is the target process; images land in the current directory:
criu dump    -t $PID -vvvv -o dump.log     # freeze + write memory/thread/fd images
criu restore -d      -vvvv -o restore.log  # -d: detach and resume from the images

# For a process attached to a terminal (a shell job):
criu dump    -t $PID --shell-job -vvvv -o dump.log
criu restore -d      --shell-job -vvvv -o restore.log
```

**DMTCP** (Distributed MultiThreaded Checkpointing) wraps launch and restart for a whole application without recompilation:

```bash
dmtcp_coordinator &            # coordination point
dmtcp_launch ./a.out &         # run under DMTCP
dmtcp_command --checkpoint     # writes ckpt_*.dmtcp + a restart script
./dmtcp_restart_script.sh      # resume
```

Applied to a running dataflow rather than a set of operating-system processes, the same recovery line appears as Apache Flink's exactly-once state: distributed snapshots driven by stream *barriers*.

## Pitfalls

- **Retaining one checkpoint per process under uncoordinated checkpointing leaves no recovery line.** With a single image each, the only consistent set may be the initial states, so a single failure discards all work.
- **Treating in-flight messages as inconsistency.** A send recorded without the matching receive is a legitimate state; deleting or re-sending such messages during recovery produces duplicates instead of fixing anything.
- **Replay under a violated PWD assumption diverges silently.** Reading a clock, a random source, a thread-scheduling order or an uncaptured signal introduces a nondeterministic event with no determinant, so the replayed state differs from the lost one without any error being raised.
- **Omitting incarnation numbers admits messages from a previous life.** A restarted process can receive messages addressed to its pre-failure incarnation and apply them to replayed state, and no checkpoint content reveals the mistake.
- **Optimistic logging without dependency tracking loses orphans.** Determinants buffered in volatile memory vanish on crash, and surviving peers that consumed the corresponding messages remain in states no replay can justify unless recovery rolls them back too.
- **Checkpointing a process whose external resources are not captured.** Open sockets, file descriptors to deleted files, and terminal attachments are part of the process state; restoring an image without them fails at restore time rather than at dump time.
