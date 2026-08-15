---
title: "Bracha's Byzantine reliable broadcast: the double-echo quorum protocol"
date: 2026-07-31
track: distributed-systems
summary: "Reliable broadcast is the primitive that sits below Byzantine consensus. Bracha's three-phase send/echo/ready protocol, its exact quorum thresholds, and why n > 3f is the price of asynchrony."
reading_time: 6
tags: [distributed-systems, byzantine-fault-tolerance, broadcast, quorums, consensus, asynchrony]
sources:
  - title: "Bracha, Asynchronous Byzantine Agreement Protocols (Inf. Comput. 1987)"
    url: "https://www.sciencedirect.com/science/article/pii/089054018790054X"
  - title: "Decentralized Thoughts: Living with Asynchrony — Bracha's Reliable Broadcast"
    url: "https://decentralizedthoughts.github.io/2020-09-19-living-with-asynchrony-brachas-reliable-broadcast/"
  - title: "EPFL DCL: Byzantine Broadcasts and Randomized Consensus (course notes)"
    url: "https://dcl.epfl.ch/site/_media/education/sdc_byzconsensus.pdf"
  - title: "Can Bölük: Optimizing Bracha's Reliable Broadcast (2025)"
    url: "https://blog.can.ac/2025/12/25/optimizing-brachas-reliable-broadcast/"
---

**Gist.** A designated sender must deliver one value to every honest node even when the sender equivocates — sending `A` to some receivers and `B` to others — and when the network delays messages arbitrarily. **Byzantine reliable broadcast (BRB)** solves this with two nested quorum gates over three message types, so that no two honest nodes can ever deliver different values and no honest node can be left behind once one has delivered. The cost is `O(n^2)` messages per broadcast and a resilience bound of `n > 3f`, where `f` bounds the number of Byzantine nodes.

## The specification

Fix `n` nodes, of which at most `f` are Byzantine (arbitrarily faulty, including colluding and lying). BRB with a designated sender `s` broadcasting value `v` guarantees three properties:

- **Validity.** If `s` is honest and broadcasts `v`, every honest node eventually delivers `v`.
- **Agreement (consistency).** No two honest nodes deliver different values.
- **Totality.** If *any* honest node delivers a value, then *every* honest node eventually delivers a value.

The omission is deliberate: when `s` is Byzantine, honest nodes may deliver nothing at all. **BRB is strictly weaker than consensus precisely because termination is not required under a faulty sender.** That weakening is what allows a fixed, deterministic protocol with no leader election, no round structure and no randomisation to run in a fully asynchronous network, where the Fischer–Lynch–Paterson (FLP) impossibility result forbids deterministic consensus. Gabriel Bracha established the protocol in *Asynchronous Byzantine Agreement Protocols* (Information and Computation 75(2):130–143, 1987).

## The state machine

Each node maintains, per broadcast instance: the set of senders from which an `ECHO` for each value has been received, the same for `READY`, a boolean recording whether it has already emitted `READY`, and a boolean recording delivery. **All gates are on the cardinality of sets of distinct senders, never on message counts** — otherwise a single Byzantine node repeating a message would inflate a quorum on its own.

Assume `n > 3f`, so at the tight bound `n = 3f+1` a quorum of `n−f` equals `2f+1`.

```text
# Sender s, on brb_broadcast(v):
    send <SEND, v> to all nodes

# Every node p:
upon receiving <SEND, v> from s (first time):
    send <ECHO, v> to all nodes

upon receiving <ECHO, v> from n-f distinct nodes (= 2f+1):
    if not yet sent READY:
        send <READY, v> to all nodes

upon receiving <READY, v> from f+1 distinct nodes:   # amplification
    if not yet sent READY:
        send <READY, v> to all nodes

upon receiving <READY, v> from n-f distinct nodes (= 2f+1):   # delivery
    brb_deliver(v)
```

Three thresholds carry the proof:

- **`2f+1` echoes → send `READY`.** A node vouches for `v` only after a Byzantine quorum has echoed it. Any two sets of `2f+1` distinct nodes drawn from `n = 3f+1` intersect in at least `f+1` nodes, so **the intersection contains at least one honest node**, and an honest node echoes at most once. Two conflicting values therefore cannot both gather `2f+1` echoes. This is where **agreement** originates.
- **`f+1` readies → send `READY` (amplification).** `f+1` distinct `READY` senders include at least one honest node, so at least one honest node has already passed a gate for `v`. Relaying is then safe, and it carries nodes that never accumulated the echo quorum — for instance because the Byzantine sender withheld `SEND` from them.
- **`2f+1` readies → deliver.** A delivery quorum of `2f+1` contains **at least `f+1` honest nodes that have sent `READY` for `v`**. Those `f+1` honest readies reach every honest node under eventual message delivery, each of which then hits the amplification gate and emits `READY` itself. Every honest node consequently accumulates `2f+1` readies. That is **totality**, and it holds without any assumption on message timing.

## Why n > 3f

The bound follows from the totality argument. The delivery quorum has size `n−f`; removing the up-to-`f` Byzantine members leaves `n−2f` honest nodes committed to `v`. Amplification requires `f+1` distinct readies, so totality needs `n−2f ≥ f+1`, that is `n ≥ 3f+1`.

At `n = 3f` the quorum would be `2f`, whose honest remainder could be as small as `f` — one short of the amplification threshold. A Byzantine sender could then wedge the system in a state where some honest nodes have delivered and the rest never will, violating totality. **The extra node is what guarantees at least one honest node in every quorum intersection and enough honest readies to trigger amplification everywhere.**

### Implementation sketch (Scala)

```scala
enum Msg[V]:
  case Send(v: V)
  case Echo(v: V)
  case Ready(v: V)

final class Brb[V](n: Int, f: Int, send: (Int, Msg[V]) => Unit, deliver: V => Unit):
  private val echoes  = collection.mutable.Map.empty[V, Set[Int]].withDefaultValue(Set.empty)
  private val readies = collection.mutable.Map.empty[V, Set[Int]].withDefaultValue(Set.empty)
  private var sentEcho, sentReady, delivered = false

  private def broadcast(m: Msg[V]): Unit = (0 until n).foreach(send(_, m))

  def receive(from: Int, m: Msg[V]): Unit = m match
    case Msg.Send(v) if !sentEcho =>          // only the first SEND is honoured
      sentEcho = true; broadcast(Msg.Echo(v))

    case Msg.Echo(v) =>
      echoes(v) = echoes(v) + from            // set semantics: replays cannot inflate a quorum
      if echoes(v).size >= n - f then emitReady(v)

    case Msg.Ready(v) =>
      readies(v) = readies(v) + from
      if readies(v).size >= f + 1 then emitReady(v)
      if readies(v).size >= n - f && !delivered then
        delivered = true; deliver(v)

    case _ => ()

  private def emitReady(v: V): Unit =
    if !sentReady then { sentReady = true; broadcast(Msg.Ready(v)) }
```

`sentReady` is a single flag rather than a per-value flag: **a node vouches for at most one value per instance**, which is what makes the intersection argument for agreement apply to readies as well as echoes.

## Position in the stack

One BRB instance per sender supports Byzantine-safe reliable multicast for command replication; the same two-gate quorum structure recurs in PBFT-style and HotStuff-style protocols, and DAG-based Byzantine fault-tolerant systems such as Narwhal and Bullshark disseminate each vertex through a reliable-broadcast-style primitive before ordering is decided.

The standing cost is quadratic: every node echoes and readies to every other. Can Bölük's 2025 write-up describes optimisations to the protocol, and a line of work reduces communication with threshold signatures; such variants keep Bracha's `n = 3f+1` resilience and its echo/ready skeleton.

## Pitfalls

- **Counting messages instead of distinct senders.** A single Byzantine node replaying `ECHO` reaches any count-based threshold alone, and agreement collapses; the gates must be on set cardinality.
- **Making `sentReady` per value.** A node that emits `READY` for two conflicting values destroys the intersection argument, and two honest nodes can then deliver different values.
- **Honouring more than one `SEND` from the sender.** An equivocating sender that gets a node to echo both `A` and `B` breaks the "an honest node echoes at most once" premise on which agreement rests.
- **Treating BRB as consensus.** Under a faulty sender no honest node need ever deliver, so a caller that blocks on delivery hangs indefinitely with no timeout to detect it.
- **Sharing state across instances.** The counters are per broadcast instance; reusing them lets one sender's echoes satisfy another's quorum.
- **Deploying at `n = 3f`.** Totality fails: some honest nodes deliver while the remainder never reach the amplification threshold, and the divergence is permanent rather than transient.
