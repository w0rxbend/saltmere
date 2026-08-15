---
title: "Virtual Synchrony: Ordering Membership and Messages Together"
date: 2026-07-31
track: distributed-systems
summary: "View-synchronous group communication ties reliable multicast to membership changes: a message is delivered in the view it was sent in, or not at all. The guarantee, the flush barrier that enforces it, and a comparison with a modern Raft log."
reading_time: 6
tags: [distributed-systems, group-communication, virtual-synchrony, multicast, consensus, replication]
sources:
  - title: "Birman & Joseph, Exploiting Virtual Synchrony in Distributed Systems (SOSP 1987)"
    url: "https://www.cs.cornell.edu/home/rvr/sys/p123-birman.pdf"
  - title: "Birman, A History of the Virtual Synchrony Replication Model"
    url: "https://www.cs.cornell.edu/ken/History.pdf"
  - title: "van Steen & Tanenbaum, Distributed Systems, 4th ed. (free digital copy)"
    url: "https://www.distributed-systems.net/index.php/books/ds4/"
  - title: "The Horus Project (Cornell)"
    url: "https://www.cs.cornell.edu/Info/Projects/HORUS/arpa_main96a.html"
  - title: "Vsync (formerly Isis2) library"
    url: "https://en.wikipedia.org/wiki/Vsync_(library)"
---

**Gist.** A reliable multicast and a membership change can race: while a message is in flight, a member crashes or joins, and the surviving replicas end up disagreeing about who was supposed to have received it. Virtual synchrony removes the race by making group membership an agreed-upon sequence of **views** and requiring that a message multicast in a view be delivered to all surviving members of that view before any of them installs the next one. The cost is a **barrier at every view change**: no survivor may advance until it has exchanged and merged the set of not-yet-stable messages with every other survivor.

Virtual synchrony was introduced by Ken Birman and Thomas Joseph in *Exploiting Virtual Synchrony in Distributed Systems* (SOSP 1987), and realised in the Isis toolkit at Cornell. van Steen and Tanenbaum treat it in the fault-tolerance chapter of *Distributed Systems* (4th ed.), alongside reliable multicast.

## Views: the unit everything hangs off

A process group has a **view**: the agreed-upon set of members at a moment in time, tagged with a view identifier. Every process in the group installs the same sequence of views `G0, G1, G2, ...`. A view change occurs when a process joins, leaves, or is detected as crashed by the group's failure detector.

The hazard is the interleaving of a multicast with a membership change. If node A multicasts `m` and node C then crashes, some receivers may account for `m` against the old view and others against the new one. The replicas then hold different answers to the question "which processes were obliged to receive `m`?", and any recovery step that reasons from that set — retransmission, state transfer to a joiner, quorum accounting — diverges.

## The view-synchronous guarantee

The model forbids exactly that interleaving:

> A message multicast in view `Gi` is delivered to **all** non-faulty members of `Gi`, or to **none** of them — and always *before* any of them installs the next view `Gi+1`.

Two consequences follow:

- **Same-view delivery.** Every process that delivers `m` delivers it in the same view. Delivery never straddles a view boundary.
- **Atomicity with respect to membership.** All members that survive into `Gi+1` agree on the exact set of messages delivered during `Gi`. The view change acts as a **barrier that flushes in-flight multicasts**.

The guarantee is not a delivery guarantee. If the sender crashes part-way through a multicast, the group is permitted to settle on "delivered everywhere" or on "delivered nowhere". **Either outcome is legal; disagreement between survivors is not.** This is the weaker property that makes the model implementable without running consensus on every message.

## The flush barrier

The enforcement mechanism is a flush protocol run at each view change. Every process retains the messages it has received in the current view that are not yet known to be **stable** — that is, not yet known to have been received by every member.

```text
state:
  view     = {members, id}     # current installed view
  unstable = {}                # msgs received in this view, not yet globally stable

on deliver-request for multicast m (view-id v):
  if v == view.id:
    add m to unstable
    deliver m in causal/total order   # per the multicast's ordering class

on view-change to proposed view v':
  # BARRIER — do not install v' yet
  send FLUSH(my unstable set) to (members(view) ∩ members(v'))
  wait until FLUSH received from every process surviving into v'

  # completeness: adopt any message a peer saw that I missed
  for each m in union(all peers' unstable sets):
    if not yet delivered: deliver m

  install view = v'             # everyone crosses the barrier with the same msg set
  unstable = {}
```

The invariant is established by the union step. Every survivor contributes its unstable set, every survivor delivers the union, and only then does any of them install `v'`. **The state at the instant of installation is therefore identical across survivors with respect to delivered messages.** A message held only by a process that does not survive into `v'` is never in any survivor's union, so the group agrees it was delivered nowhere — the permitted outcome above.

The **ordering** of messages within a view is a separate, composable concern. The Isis primitives factor it out: **CBCAST** for causal order, **ABCAST** for total order, and **GBCAST** for the view and membership changes themselves. An application selects the weakest ordering it tolerates; view synchrony holds regardless of which is chosen.

### Implementation sketch (Scala)

The barrier, reduced to its load-bearing part: a process refuses to install the proposed view until every survivor's unstable set has arrived, then delivers the union before advancing.

```scala
final case class View(id: Long, members: Set[NodeId])
final case class Msg(id: MsgId, viewId: Long, payload: Vector[Byte])   // value equality: Set[Msg] must dedupe by content

final class GroupMember(self: NodeId, deliver: Msg => Unit):
  private var view: View          = View(0, Set(self))
  private var unstable: Set[Msg]  = Set.empty
  private var delivered: Set[MsgId] = Set.empty

  def onMulticast(m: Msg): Unit =
    // messages tagged with a stale view id belong to a flush that already closed
    if m.viewId == view.id && !delivered.contains(m.id) then
      unstable += m
      delivered += m.id
      deliver(m)

  /** Blocks until every survivor has flushed; the union is delivered before install. */
  def onViewChange(proposed: View, flush: (View, Set[Msg]) => Map[NodeId, Set[Msg]]): Unit =
    val survivors = view.members intersect proposed.members
    val gathered  = flush(proposed, unstable)          // returns once all survivors replied
    require(gathered.keySet == survivors - self)

    for m <- gathered.values.flatten.toSet if !delivered.contains(m.id) do
      delivered += m.id
      deliver(m)                                       // completeness before the barrier lifts

    view = proposed
    unstable = Set.empty
```

`flush` is the blocking gather: it must not return while any survivor's reply is outstanding, because returning early is precisely what breaks the invariant.

## The lineage

The model has been re-implemented repeatedly, each time factoring the stack differently:

- **Isis** — the original Cornell toolkit; virtual synchrony as group replication.
- **Horus** — Robbert van Renesse's demonstration that the protocol stack can be built as a composition of small, swappable microprotocols.
- **Ensemble** — Horus rewritten in OCaml by Mark Hayden, a form that admitted formal verification of the layers.
- **Isis2 / Vsync** — Birman's C#/.NET library, later renamed Vsync.

## Contrast with modern consensus

Consensus protocols (Raft, Zab, Multi-Paxos) collapse membership and message order into a single totally ordered replicated log behind a leader: one log, one order. This is the shape adopted by etcd and ZooKeeper, and it yields a linearizable sequence of commands.

Virtual synchrony **separates membership agreement from message ordering**. An agreement protocol runs only at view changes; between them, multicasts flow with whatever ordering class the application requested, frequently causal order, which admits more concurrency than funnelling every message through a total order. The resulting model is weaker and less uniform: "the set everyone agreed they delivered in this view" does not name a position in a log, so there is no index to reason about or to resume from.

The selection criterion follows from that difference. A single authoritative log with linearizable semantics calls for consensus. A group with churning membership, mostly causal traffic, and no need for a global sequence number per message is the case view-synchronous group communication was built for. Derecho carries the same virtual-synchrony model over remote direct memory access (RDMA).

## Pitfalls

- **Treating the guarantee as a delivery guarantee.** A sender that crashes mid-multicast may leave `m` delivered nowhere; code that assumes an acknowledged send implies group-wide delivery loses the message silently.
- **Returning from the flush before all survivors reply.** A timeout that gives up on a slow-but-alive survivor lets that process install `v'` with a different delivered set, which is the exact divergence the barrier exists to prevent.
- **Accepting a multicast tagged with the previous view identifier after the barrier has closed.** The message is delivered by one straggler and by no one else, breaking same-view delivery.
- **Failure detector false positives.** Each spurious suspicion forces a view change, and each view change forces a barrier; under an aggressive timeout the group spends its time flushing rather than multicasting.
- **Assuming a weaker ordering class is free.** CBCAST orders causally related messages only; concurrent updates to the same replicated item are delivered in different orders at different members, and the application must reconcile them itself.
- **Expecting a log position.** There is no per-message global index, so recovery and state transfer must be expressed in terms of views and delivered sets rather than "resume from offset N".
