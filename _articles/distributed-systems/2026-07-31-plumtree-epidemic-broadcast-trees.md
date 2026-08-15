---
title: "Plumtree: Gossip That Self-Optimizes Into a Spanning Tree"
date: 2026-07-31
track: distributed-systems
summary: "How Plumtree keeps flooding gossip's fault tolerance while collapsing its redundancy into a spanning tree — eager/lazy push, PRUNE, and GRAFT-based healing."
reading_time: 6
tags: [distributed-systems, gossip, broadcast, epidemic-protocols, plumtree, riak]
sources:
  - title: "Epidemic Broadcast Trees (Leitão, Pereira, Rodrigues, SRDS 2007)"
    url: "https://asc.di.fct.unl.pt/~jleitao/pdf/srds07-leitao.pdf"
  - title: "Riak Core Broadcast (basho/riak_core wiki)"
    url: "https://github.com/basho/riak_core/wiki/Riak-Core-Broadcast"
  - title: "Erlang implementation of the Plumtree protocol"
    url: "https://github.com/lrascao/plumtree"
  - title: "Plumtree — epidemic broadcast trees (Bartosz Sypytkowski)"
    url: "https://www.bartoszsypytkowski.com/plumtree/"
  - title: "plum_db: Epidemic Broadcast Trees + Partisan (Leapsight)"
    url: "https://github.com/Leapsight/plum_db"
---

**Gist.** Flooding gossip tolerates crashes and link failures because every node forwards each new message to several random peers, but in a stable network most received messages are duplicates, so the redundancy is paid for continuously and used rarely. Plumtree — "Epidemic Broadcast Trees" by João Leitão, José Pereira and Luís Rodrigues (SRDS 2007) — starts as pure flooding and lets each redundant edge remove itself, converging on a spanning tree that carries the payload while the remaining overlay links carry only message identifiers. The cost is that repair is no longer instantaneous: a broken branch is detected by timer expiry rather than by a duplicate, so a failure adds latency proportional to the detection timeout for the affected messages.

## Two peer sets, two kinds of push

Each node partitions the neighbours supplied by a membership service (HyParView in the paper) into two sets:

- **eagerPushPeers** — receive the full payload immediately in a GOSSIP message. These links constitute the tree.
- **lazyPushPeers** — receive only the message identifier in an IHAVE announcement, batched and sent lazily. These links constitute the repair channel.

**Every neighbour begins as an eager peer**, so the first broadcast floods exactly as naive gossip does. The optimisation is driven entirely by duplicate arrivals; no node computes a tree, and no node holds a global view.

## Pruning: turning a mesh into a tree

When a node receives a GOSSIP whose message identifier it has already delivered, that arrival is evidence that **the same payload reached the node faster along another path**, which makes the incoming edge redundant for tree purposes. The receiver demotes the sender to its lazy set and replies with **PRUNE**, on receipt of which the sender performs the symmetric demotion. Both endpoints therefore agree on the classification of the edge — the invariant that keeps the eager relation symmetric.

Symmetric demotion of every cyclic edge leaves a spanning tree over the overlay, on which **each node receives each payload exactly once**.

```text
on GOSSIP(m, mID, round, sender):
    if mID not in received:
        received.add(mID); deliver(m)
        eagerPush(m, mID, round+1, exclude=sender)   # forward to eager peers
        lazyPush(mID, round+1, exclude=sender)        # announce IHAVE to lazy peers
        eagerPushPeers.add(sender)                     # keep this tree edge
    else:                                              # duplicate → redundant edge
        eagerPushPeers.remove(sender)
        lazyPushPeers.add(sender)
        send(PRUNE) -> sender

on PRUNE(sender):
    eagerPushPeers.remove(sender); lazyPushPeers.add(sender)
```

## Grafting: healing the tree with the leftover links

A tree carries no redundancy, so the loss of one interior node or one branch would leave part of the membership undelivered. The lazy links close that gap. Each node announces every message identifier to its lazy peers, so **an IHAVE for an identifier that never arrived as GOSSIP is the signal that the tree branch towards that message's source is broken or slow**.

The reaction is deliberately delayed. The node arms a timer, `timeout1`, giving the in-flight payload a chance to arrive over the tree; **if the payload arrives first, the timer is cancelled and the tree is left unchanged**, which is why a merely slow branch does not thrash the topology. If the timer fires first, the node performs a **GRAFT**: it promotes the announcing lazy peer to eager and requests the missing payload from it. One message therefore both repairs a tree edge and retrieves the data.

A second timer, `timeout2`, shorter than `timeout1`, arms a retry against the next node that announced the same identifier, so a peer that is itself dead does not stall recovery indefinitely. The set of announcers is retained in insertion order precisely so that this fallback exists.

```text
on IHAVE(mID, sender):
    if mID not in received:
        if not timer_running(mID): setup_timer(mID, timeout1)
        missing.append((mID, sender))          # remember who can supply it

on TIMER(mID):
    setup_timer(mID, timeout2)                  # arm retry against next announcer
    (mID, sender) = missing.first_for(mID)
    eagerPushPeers.add(sender)                  # re-graft this edge into the tree
    lazyPushPeers.remove(sender)
    send(GRAFT, mID) -> sender

on GRAFT(mID, sender):
    eagerPushPeers.add(sender); lazyPushPeers.remove(sender)
    if mID in received: send(GOSSIP, payload[mID], mID, ...) -> sender
```

Because healing draws on the entire remaining overlay rather than a designated backup path, node churn and partitions are routed around without configuration, and any redundancy that healing reintroduces is removed again by the ordinary PRUNE path on the next duplicate. Membership changes enter through the sampling service's `NeighborUp`/`NeighborDown` callbacks, which add to or remove from the eager set.

GOSSIP messages additionally carry a **round number**, the hop count from the source. When lazy delivery of an identifier consistently precedes eager delivery by more than a configured threshold in rounds — **the threshold is a protocol parameter, not a derived constant** — the node grafts the faster link and prunes the slower one, shortening the tree without waiting for a failure.

### Implementation sketch (Scala)

The load-bearing state is two peer sets plus the pending-announcement map; the transitions below are the eager/lazy reclassification rules only, with transport and timer scheduling elided.

```scala
type NodeId = String
type MsgId  = String

final class Plumtree(self: NodeId, send: (NodeId, Any) => Unit):
  private var eager: Set[NodeId] = Set.empty
  private var lazyPeers: Set[NodeId] = Set.empty
  private var received: Map[MsgId, (Array[Byte], Int)] = Map.empty  // payload and its round
  private var missing: Map[MsgId, List[NodeId]] = Map.empty  // announcers, in arrival order

  private def toLazy(p: NodeId): Unit = { eager -= p; lazyPeers += p }
  private def toEager(p: NodeId): Unit = { lazyPeers -= p; eager += p }

  def onGossip(id: MsgId, payload: Array[Byte], round: Int, from: NodeId): Unit =
    if received.contains(id) then
      toLazy(from); send(from, Prune(self))          // duplicate ⇒ edge is redundant
    else
      received += id -> (payload, round)
      missing -= id                                  // gap closed; no GRAFT needed
      toEager(from)
      eager.excl(from).foreach(send(_, Gossip(id, payload, round + 1, self)))
      lazyPeers.excl(from).foreach(send(_, IHave(id, round + 1, self)))

  def onIHave(id: MsgId, from: NodeId): Unit =
    if !received.contains(id) then
      missing = missing.updatedWith(id)(prev => Some(prev.getOrElse(Nil) :+ from))

  /** Invoked when timeout1 (or the timeout2 retry) expires for `id`. */
  def onTimeout(id: MsgId): Unit =
    missing.get(id).flatMap(_.headOption).foreach: announcer =>
      missing = missing.updated(id, missing(id).tail)
      toEager(announcer)
      send(announcer, Graft(id, self))

  def onGraft(id: MsgId, from: NodeId): Unit =
    toEager(from)
    received.get(id).foreach((p, r) => send(from, Gossip(id, p, r + 1, self)))
```

## Deployments

- **Riak Core** — `riak_core_broadcast` is Plumtree-based and underpins Cluster Metadata. Basho replaced HyParView with a fully connected peer service sized for the cluster sizes Riak targets, and added lazy-message queuing and periodic anti-entropy exchanges so that state missed by broadcast is still reconciled.
- **HyParView** — the membership protocol the paper pairs with Plumtree: HyParView maintains the resilient partial view, Plumtree builds the broadcast tree over it.
- **Partisan / plum_db** — Leapsight's `plum_db` reimplements the Riak Core metadata store over lasp-lang's Partisan, retaining the epidemic-broadcast-tree core.

Eager push is the steady-state path, IHAVE/GRAFT is the repair channel, and PRUNE is what makes the steady-state path cheap. The tree is never load-bearing on its own, because the full gossip overlay remains present underneath it.

## Pitfalls

- **`timeout1` set too low.** A transiently slow tree branch triggers GRAFTs before the payload arrives; the grafted edge then delivers a duplicate, which triggers a PRUNE, and the topology oscillates while paying both payload and control traffic.
- **`timeout1` set too high.** A genuinely failed interior node is not detected until the timer expires, so every message in flight through that branch is delayed by at least that interval before repair begins.
- **Asymmetric peer sets.** If a PRUNE is lost and only the receiver demotes the edge, the sender keeps eager-pushing, so the receiver keeps observing duplicates and keeps re-issuing PRUNE; the symmetry of the eager relation is what terminates the exchange.
- **Discarding the announcer list after the first GRAFT.** Retaining only one announcer per identifier means a GRAFT sent to a peer that has itself crashed has no fallback when `timeout2` fires, and the message is not recovered until another IHAVE arrives.
- **Unbounded `received` and `missing` maps.** Both are keyed by message identifier and grow with broadcast volume; without expiry, duplicate suppression and gap detection retain state for the lifetime of the process.
- **Assuming delivery is ordered.** The protocol establishes that each node receives each payload once over the tree; grafted deliveries arrive out of the tree's normal order, so applications requiring order must supply it themselves.
