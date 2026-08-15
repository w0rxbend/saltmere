---
title: "Sloppy Quorums and Hinted Handoff: Staying Writable When a Replica Is Down"
date: 2026-07-30
track: distributed-systems
summary: "A strict quorum blocks the write the moment a home replica is unreachable. Dynamo's answer — write to the first N healthy nodes and let a stand-in hold the data until the rightful owner returns — trades the overlap proof for availability. The mechanism, and how Cassandra and Riak wire it up."
reading_time: 6
tags: [dynamo, hinted-handoff, sloppy-quorum, cassandra, riak, availability]
sources:
  - title: "DeCandia et al., Dynamo: Amazon's Highly Available Key-value Store (SOSP 2007), §4.6"
    url: "https://www.allthingsdistributed.com/files/amazon-dynamo-sosp2007.pdf"
  - title: "Hints — Apache Cassandra Documentation (5.0)"
    url: "https://cassandra.apache.org/doc/latest/cassandra/managing/operating/hints.html"
  - title: "Replication Properties — Riak KV Documentation"
    url: "https://docs.riak.com/riak/kv/latest/developing/app-guide/replication-properties/index.html"
  - title: "Hinted Handoff and GC Grace Demystified — The Last Pickle"
    url: "https://thelastpickle.com/blog/2018/03/21/hinted-handoff-gc-grace-demystified.html"
---

**Gist.** A strict quorum with `R + W > N` guarantees that every read set intersects every write set, but only while the `N` designated replicas of a key are reachable; a single unreachable home replica forces the coordinator to block or to fail the write. A **sloppy quorum** relaxes *which* nodes count, directing the operation at the first `N` **healthy** nodes of the key's preference list, and **hinted handoff** records, on each stand-in, the identity of the replica it is covering for so the data can be replayed on recovery. The cost is that the intersection argument is suspended for the duration of the failure: the acknowledged write set and a later read set can be disjoint, so an acknowledged write can be invisible until the hint is delivered.

The [quorum article](/articles/distributed-systems/2026-07-25-quorum-replication-r-plus-w/) left the strict rule with one caveat: the overlap proof holds only as long as the *right* replicas are up.

## Sloppy quorum: the preference list, not the home nodes

Consistent hashing assigns every key a **preference list** — a ring-ordered sequence of nodes eligible to hold it, deliberately longer than `N` so stand-ins exist. A strict quorum writes to the first `N` distinct nodes on that list and no others. Dynamo's sloppy quorum performs "all read and write operations on the first `N` *healthy* nodes from the preference list" (§4.6). The qualifier *healthy* is the entire change: an unreachable node does not block the operation, it is skipped, and the next reachable node further down the list occupies its slot.

The quorum threshold itself is untouched. The write still requires `W` acknowledgements, so **the `R`/`W` tuning knobs keep their strict-quorum meaning** — but the members supplying those acknowledgements need not be the object's rightful owners. Availability through a node failure is obtained without altering `N`, `W` or `R`.

### Implementation sketch (Scala)

The coordinator's walk down the preference list, and the bookkeeping that records each substitution:

```scala
type Node = String

final case class Plan(targets: Vector[Node], hints: Map[Node, Node])

/** `hints(stand-in) = intended owner`; empty when the quorum is strict. */
def planWrite(prefs: Vector[Node], n: Int, alive: Node => Boolean): Plan =
  prefs.iterator
    .filter(alive)
    .take(n)
    .zipWithIndex
    .foldLeft(Plan(Vector.empty, Map.empty)) { case (plan, (node, slot)) =>
      // slot `i` belongs to the i-th node of the *unfiltered* list
      val intended = prefs(slot)
      val hints =
        if node == intended then plan.hints else plan.hints + (node -> intended)
      Plan(plan.targets :+ node, hints)
    }

def coordinateWrite(
    prefs: Vector[Node], n: Int, w: Int,
    alive: Node => Boolean,
    send: (Vector[Node], Map[Node, Node]) => Int  // returns acks received
): Boolean =
  val plan = planWrite(prefs, n, alive)
  // fewer than `n` healthy nodes may exist; `w` is still the bar
  send(plan.targets, plan.hints) >= w
```

The walk stops once `n` healthy nodes are collected or the preference list is exhausted; in the latter case fewer than `n` targets are contacted and the `w` threshold can fail outright.

## Hinted handoff: the stand-in records whom it covers

Availability is half the requirement; the data must still reach its home node. When node `D` accepts a write belonging to the unreachable node `A`, "the replica sent to D will have a hint in its metadata that suggests which node was the intended recipient" (§4.6). `D` does not merge that copy into its own dataset. Hinted replicas are kept "in a separate local database that is scanned periodically", and "upon detecting that A has recovered, D will attempt to deliver the replica to A". On success, "D may delete the object from its local store without decreasing the total number of replicas."

The state machine is therefore: **skip the unreachable node → store a hint on a stand-in → replay to the rightful owner on recovery → delete the hint**. Dynamo's own example uses nodes `A`–`D` with `N=3`: a write intended for a temporarily unavailable `A` lands on `D` carrying a hint, "to maintain the desired availability and durability guarantees."

The separate store is load-bearing. Because the hinted replica is not part of `D`'s own key range, `D` does not serve it to readers and does not consider itself an owner; the copy exists solely as a queued delivery.

## The trade-off: durability up, intersection guarantee down

A sloppy quorum buys durability — the write is resident on `N` machines somewhere — at the cost of the intersection property. During the failure window the write set (`B`, `C`, `D`) and a later read set that includes the recovered `A` can be **disjoint**, so a reader can miss a write that was already acknowledged. The `R + W > N` pigeonhole argument assumes a fixed membership; a sloppy quorum moves the membership, and the argument moves with it. The session guarantees "read own writes" and "monotonic reads" can both be violated until the hint is replayed.

Hinted handoff closes the gap, but **eventually, and only if the stand-in survives long enough to deliver**. A stand-in that fails permanently before replay removes a copy that the rightful owner never received. Read repair and full anti-entropy repair — Merkle-tree comparison between replicas — are the backstop; hinted handoff alone is not one.

## Cassandra: hints as flat files under a window

Cassandra implements this scheme directly. When a replica is unreachable at write time the coordinator stores a **hint** — the target replica's identifier, a hint identifier carrying the creation time, the message serialization version, and the serialized mutation — as flat files under `hints_directory` (`$CASSANDRA_HOME/data/hints`), replaying them when the node returns. The relevant `cassandra.yaml` settings:

```yaml
hinted_handoff_enabled: true      # master switch
max_hint_window: 3h               # stop generating hints past this downtime
hints_directory: /var/lib/cassandra/hints
hints_flush_period: 10000ms       # buffer -> disk cadence
max_hints_file_size: 128MiB
hinted_handoff_throttle: 1024KiB  # per second, per delivery thread
hints_compression:
  - class_name: LZ4Compressor
```

The load-bearing parameter is **`max_hint_window`, default `3h`**. Hints are retained for up to that much downtime; a node returning inside the window is replayed automatically. Past the window Cassandra stops accumulating hints, and "the destination replica will be permanently out of sync until either read-repair or full/incremental anti-entropy repair propagates the mutation." The cap bounds the hint backlog a coordinator can accumulate for a single unreachable node.

Operational controls are exposed through `nodetool`:

```bash
nodetool statushandoff     # report whether handoff is enabled
nodetool disablehandoff    # e.g. before a planned, lengthy node outage
nodetool enablehandoff
nodetool truncatehints     # drop accumulated hints (all, or per-endpoint)
```

A consequence documented by [The Last Pickle](https://thelastpickle.com/blog/2018/03/21/hinted-handoff-gc-grace-demystified.html): because a node down past the window receives no hints, repair must run within `gc_grace_seconds` or a deletion can be resurrected. Hints are a convenience layer, not a substitute for repair.

## Riak: sloppy by default, strict on request

Riak makes the same trade-off configurable per request. It normally writes to primary vnodes "but in case of failure, those operations will go to failover nodes in order to comply with the R and W values" — a sloppy quorum, with the failed vnode's data returned by hinted handoff on recovery. Riak also permits opting out: setting `pr` (primary read) or `pw` (primary write) above zero "produces a mode of operation called *strict quorum*," requiring responses from the primary vnodes themselves. That disables the fallback, giving "a higher probability that reads or writes will fail because primary vnodes are unavailable," in exchange for being "more likely to receive the most up-to-date values."

| | Strict quorum | Sloppy quorum + hinted handoff |
|---|---|---|
| Quorum membership | first `N` home nodes | first `N` *healthy* nodes on the preference list |
| One home node down | write blocks or fails | write succeeds on a stand-in |
| Intersection guarantee | holds (`R + W > N`) | suspended until hints replay |
| Convergence backstop | — | hint replay, then read repair / anti-entropy |
| Cassandra / Riak knob | `pr`/`pw` > 0 (Riak) | default; `hinted_handoff_enabled`, `max_hint_window` |

A strict quorum optimizes for the intersection proof; a sloppy quorum optimizes for the write completing. Hinted handoff reconciles the two — bounded by a window, and backed by repair.

## Pitfalls

- **Treating hinted handoff as a durability mechanism.** A stand-in holding the only hinted copy is a single point of failure for that write; losing it before replay loses the update, because the rightful owner never received it and no other replica holds it.
- **Assuming `R + W > N` still implies that a client reads its own writes.** During the failure window the acknowledged write set and the subsequent read set can be disjoint, so a client can read a value older than the one its own write returned success for.
- **Leaving a node down longer than `max_hint_window` and skipping repair.** Cassandra stops generating hints past the window, and the destination replica stays out of sync until read repair or anti-entropy repair propagates the mutation.
- **Missing the `gc_grace_seconds` deadline after a long outage.** A replica that never received the hint for a deletion, and is not repaired within the grace period, can resurrect the deleted row.
- **Enabling handoff during a planned, lengthy outage.** Coordinators accumulate hint files for the whole window; `nodetool disablehandoff` before the outage avoids the backlog and the replay burst on return.
- **Setting Riak's `pw` above zero and expecting unchanged availability.** Requiring primary vnodes disables the failover path, so writes fail whenever a primary is unavailable — the documented cost of strict quorum in Riak.
