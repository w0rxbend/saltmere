---
title: "Virtual Synchrony: Ordering Membership and Messages Together"
date: 2026-07-31
track: distributed-systems
summary: "View-synchronous group communication ties reliable multicast to membership changes: a message is delivered in the view it was sent in, or not at all. Here's the guarantee, some pseudocode, and how it compares to a modern Raft log."
reading_time: 5
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

Most people reach for Raft the moment they need replicated state. But there's an older idea that solves a slightly different problem, and it's still the cleanest way to think about "a group of processes that all see the same thing": **virtual synchrony**, introduced by Ken Birman and Thomas Joseph in *Exploiting Virtual Synchrony in Distributed Systems* (SOSP 1987) and shipped in the Isis toolkit that same year.

The core move is to make **group membership** a first-class, agreed-upon fact, and then order message delivery *relative to changes in that membership*. van Steen & Tanenbaum cover this in the fault-tolerance chapter of *Distributed Systems* (4th ed.), and it's worth reading alongside their treatment of reliable multicast.

## Views: the unit everything hangs off

A process group has a **view**: the agreed-upon set of members at a moment in time, tagged with a view identifier. Every process in the group installs the same sequence of views `G0, G1, G2, ...`. A view change happens when a process joins, leaves, or is detected as crashed.

The problem virtual synchrony solves is the race between *sending a multicast* and *the membership changing underneath it*. If node A multicasts `m` and then C crashes, some receivers might deliver `m` "in" the old view and some "in" the new one. Now your replicas disagree about who was supposed to have received `m`. That's the split-brain gap.

## The view-synchronous guarantee

Virtual synchrony forbids exactly that. The guarantee, in one sentence:

> A message multicast in view `Gi` is delivered to **all** non-faulty members of `Gi`, or to **none** of them — and always *before* any of them installs the next view `Gi+1`.

Two consequences fall out:

- **Same-view delivery.** Every process that delivers `m` delivers it in the same view. Message delivery never straddles a view boundary.
- **Atomic-with-respect-to-membership.** All members that survive into `Gi+1` agree on the exact set of messages delivered during `Gi`. A view change acts as a **barrier** that flushes in-flight multicasts.

Crucially this does *not* require the message to be delivered — if the sender crashes mid-multicast, the group is allowed to agree that `m` was delivered to everyone *or* to no one. Either outcome is legal; disagreement is not.

## Pseudocode: the flush barrier

The mechanism is a flush protocol run at each view change. Every process keeps the set of messages it has received in the current view but that aren't yet known-stable (received by everyone).

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

Because every survivor exchanges its unstable set and delivers the union before installing `v'`, they all enter the new view having delivered an identical set of messages. That is virtual synchrony.

The *ordering* of messages within a view is a separate, composable concern. The Isis primitives split it out: **CBCAST** (causal order), **ABCAST** (total order), and **GBCAST** (used for the view/membership changes themselves). You pick the weakest ordering your application tolerates — causal is far cheaper than total — and view-synchrony still holds.

## The lineage

This idea has been re-implemented for four decades, each time factoring the stack differently:

- **Isis** (1987) — the original toolkit; virtual synchrony as bare-bones group replication.
- **Horus** — Robbert van Renesse showed the protocol stack could be built as a composition of tiny, swappable microprotocols.
- **Ensemble** — Horus rewritten in OCaml by Mark Hayden, which invited formal verification of the layers.
- **Isis2 / Vsync** (2010) — Birman's C#/.NET library (renamed from "Isis2" to avoid the obvious naming problem).

## Contrast with modern consensus

If you squint, view-synchrony and a Raft/Paxos log are solving overlapping problems, but they carve reality differently.

Consensus (Raft, Zab, Multi-Paxos) collapses *everything* — membership and message order — into a single totally-ordered replicated log behind a leader. One log, one order, strong and simple. That's why it dominates today: etcd, ZooKeeper, and friends all give you a linearizable sequence of commands.

Virtual synchrony **separates** membership agreement from message ordering. You pay for a real agreement protocol only at view changes; between them, multicasts flow with whatever ordering you asked for (often just causal), which can be dramatically cheaper and more concurrent than funnelling every message through a total order. The trade is a more subtle model: "what everyone agreed they delivered in this view" is weaker and more flexible than "position N in the log."

Rule of thumb: reach for **consensus** when you need a single authoritative log and linearizable semantics. Reach for **view-synchronous group communication** when you have a group that mostly gossips, membership genuinely churns, and paying total-order costs on every message is wasteful. The modern high-throughput descendant, Derecho, pushes the same virtual-synchrony model over RDMA — proof the idea still scales.

**Try next:** Implement the flush-barrier pseudocode above as a small simulation (three in-process actors, a message queue you can reorder, and a "kill" button), then assert the invariant after every view change: all survivors delivered the identical set of messages. Watching that invariant hold while you drop and crash members is the fastest way to internalize what "virtual" in virtual synchrony actually buys you.
