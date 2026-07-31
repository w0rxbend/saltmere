---
title: "Plumtree: Gossip That Self-Optimizes Into a Spanning Tree"
date: 2026-07-31
track: distributed-systems
summary: "How Plumtree keeps flooding gossip's fault tolerance while collapsing its redundancy into a spanning tree — eager/lazy push, PRUNE, and GRAFT-based healing."
reading_time: 5
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

Gossip broadcast is loved for one reason: it degrades gracefully. Every node forwards each new message to `f` random peers (eager push), so a message reaches everyone even as nodes crash and links flap. The cost is embarrassing redundancy — in a stable network, most GOSSIP messages a node receives are duplicates it already has. You pay for resilience you aren't currently using.

Plumtree — "Epidemic Broadcast Trees" by João Leitão, José Pereira, and Luís Rodrigues (SRDS 2007) — resolves the trade-off instead of picking a side. It starts as pure flooding and *lets the redundant links prune themselves away*, converging on a spanning tree for the payload while keeping the full gossip overlay in reserve to repair that tree.

## Two peer sets, two kinds of push

Each node partitions its neighbors (supplied by a membership service like HyParView) into two sets:

- **eagerPushPeers** — get the full payload immediately (GOSSIP). These links form the tree.
- **lazyPushPeers** — get only the message ID (IHAVE), batched and sent lazily. These are the safety net.

Initially every neighbor is an eager peer, so the first broadcast floods exactly like naive gossip. The optimization happens as duplicates arrive.

## Pruning: turning a mesh into a tree

When a node receives a **duplicate** GOSSIP (it already delivered that message ID), it knows the link it arrived on is redundant — the message reached it faster by another path. So it demotes the sender to lazy and replies with a **PRUNE**, telling the sender to do the same in the other direction. Both ends move the link from eager to lazy. Symmetric pruning of every cyclic edge leaves precisely a spanning tree, where each node receives each payload exactly once.

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

A tree has no redundancy, so a single broken branch would partition delivery — unacceptable. This is where the lazy links earn their keep. Every node also announces each message ID to its lazy peers via IHAVE. If a node sees an **IHAVE for a message it never received via the tree**, that gap signals a broken (or slow) branch.

Rather than react instantly, it arms a timer (`timeout1`) to let the in-flight payload arrive. If the timer fires first, the node **GRAFTs**: it promotes the announcing lazy peer to eager and requests the missing payload. A GRAFT both repairs the tree edge and pulls the data. A second, shorter timer (`timeout2`, roughly one RTT) retries against the next node that announced the same ID, so a dead peer doesn't stall recovery.

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

Because healing draws on the *entire* remaining overlay, not a fixed backup path, partitions and node churn get routed around automatically — and any redundant branches the healing introduces get pruned right back out by the normal PRUNE path. Membership churn plugs in through the sampling service's `NeighborUp`/`NeighborDown` callbacks, which add or drop entries in the eager set. Plumtree also carries a round number on messages so that if lazy delivery consistently beats eager delivery by a threshold (the paper uses 3 for a single sender, 7 for many), it proactively grafts the faster link and prunes the slower one, shortening the tree.

## Where it runs in production

- **Riak Core** — `riak_core_broadcast` is Plumtree-based and underpins Cluster Metadata. Basho swapped HyParView for a fully-connected peer service (tuned for ~5–100 nodes) and layered on lazy-message queuing, anti-entropy, and historical delivery for late-joining nodes.
- **HyParView** — Plumtree's natural partner: HyParView maintains the resilient partial-view membership; Plumtree builds the efficient broadcast tree on top of it.
- **Partisan / plum_db** — Leapsight's `plum_db` reimplements the Riak Core metadata store over lasp-lang's Partisan, keeping the epidemic-broadcast-tree core.

The mental model worth keeping: eager push is your *steady-state fast path*, lazy push (IHAVE/GRAFT) is your *repair channel*, and PRUNE is what makes the fast path cheap. You get a tree's efficiency with gossip's failure semantics because the tree is never load-bearing on its own — the gossip overlay is always underneath it.

**Try next:** Clone `lrascao/plumtree`, wire up a 7-node cluster, and instrument the eager/lazy peer sets. Broadcast a message, watch PRUNEs collapse the mesh into a tree, then kill an interior node mid-broadcast and log the IHAVE-triggered GRAFTs that re-stitch delivery — the whole heal cycle is visible in a few dozen lines of trace.
