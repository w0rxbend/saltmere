---
title: "Sloppy Quorums and Hinted Handoff: Staying Writable When a Replica Is Down"
date: 2026-07-30
track: distributed-systems
summary: "A strict quorum blocks the write the moment a home replica is unreachable. Dynamo's answer — write to the first N healthy nodes and let a stand-in hold the data until the real owner returns — trades the clean overlap proof for availability. Here's the mechanism, and how Cassandra and Riak wire it up."
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

The [quorum article](/articles/distributed-systems/2026-07-25-quorum-replication-r-plus-w/) left the strict rule with one honest caveat: `R + W > N` gives you the clean overlap proof only as long as the *right* replicas are up. Store an object on its `N` home nodes and require `W` of them to ack. Now one home node is down. A strict quorum has two options — wait, or fail the write. Neither keeps you available. Dynamo picks a third: relax *which* nodes count as the quorum. That's a **sloppy quorum**, and **hinted handoff** is the bookkeeping that makes it eventually converge.

## Sloppy quorum: the preference list, not the home nodes

Consistent hashing gives every key a **preference list** — a ring-ordered sequence of nodes that could hold it, longer than `N` so there are stand-ins. A strict quorum writes to the first `N` distinct nodes on that list, full stop. Dynamo's sloppy quorum instead performs "all read and write operations on the first `N` *healthy* nodes from the preference list" (§4.6). The word *healthy* is the whole change: a down node doesn't block the write, it just gets skipped, and the next reachable node down the list stands in for it.

The coordinator's walk is a few lines:

```python
def coordinate_write(key, value, N, W):
    prefs = preference_list(key)        # ring-ordered, length > N
    targets, hints = [], {}
    for node in prefs:                  # walk until N healthy accept
        if len(targets) == N:
            break
        if not alive(node):
            continue
        intended = prefs[len(targets)]  # the home node this slot belongs to
        if node is not intended:
            hints[node] = intended      # node is standing in for `intended`
        targets.append(node)
    acks = send_write(targets, key, value, hints)
    return acks >= W                    # W of the N *healthy* nodes ack
```

The write still needs `W` acks, so latency and the availability slider behave as before — but the members satisfying it may not be the object's rightful owners. You stay writable through a node failure without changing `N`, `W`, or `R`.

## Hinted handoff: the stand-in remembers who it's covering for

Availability is only half the job; the data still has to reach its home node eventually. When node `D` accepts a write that belonged on the down node `A`, "the replica sent to D will have a hint in its metadata that suggests which node was the intended recipient" (§4.6). `D` doesn't mix that copy into its own dataset. It keeps hinted replicas "in a separate local database that is scanned periodically," and "upon detecting that A has recovered, D will attempt to deliver the replica to A." On success, "D may delete the object from its local store without decreasing the total number of replicas."

So the lifecycle is: skip the down node → stash a hint on a stand-in → replay to the real owner on recovery → drop the hint. Dynamo's own example is exactly nodes `A`–`D` with `N=3`: a write meant for a temporarily-down `A` lands on `D` with a hint, "to maintain the desired availability and durability guarantees."

## The tradeoff: durability up, consistency guarantee down

Sloppy quorum buys durability — the write survives on `N` machines somewhere — at the cost of the overlap guarantee that made strict quorums linearizable-ish. During the failure window the write set (`B`, `C`, `D`) and a later read set that touches the recovered `A` can be **disjoint**, so a reader can miss a write that was already acked. The `R + W > N` pigeonhole argument assumed a fixed membership; sloppy quorums move the members, and the proof moves with them. You can violate read-your-writes and monotonic reads until the hint replays. Hinted handoff is what closes the gap, but it closes it *eventually*, and only if the stand-in survives long enough to hand the data off. A stand-in that dies before replay takes the only durable-beyond-`W-1` copy with it — which is why anti-entropy repair (Merkle trees, read-repair) is the backstop, not hinted handoff alone.

## Cassandra: hints as flat files with a window

Cassandra implements exactly this. When a replica is unreachable at write time, the coordinator stores a **hint** — the target replica, the serialized mutation, a timestamp, and the Cassandra version — as flat files under `hints_directory` (`$CASSANDRA_HOME/data/hints`), and replays them when the node comes back. The knobs in `cassandra.yaml`:

```yaml
hinted_handoff_enabled: true      # master switch
max_hint_window: 3h               # stop generating hints for a node down longer than this
hints_directory: /var/lib/cassandra/hints
hints_flush_period: 10000ms       # buffer -> disk cadence
max_hints_file_size: 128MiB
hinted_handoff_throttle: 1024KiB  # per delivery thread, throttles replay
hints_compression:
  - class_name: LZ4Compressor
```

The load-bearing parameter is `max_hint_window` (default `3h`). Hints are retained for up to that much downtime; if the node returns inside the window, replay is automatic. Cross the window and Cassandra stops accumulating hints — "the destination replica will be permanently out of sync until either read-repair or full/incremental anti-entropy repair propagates the mutation." That cap exists so a long-dead node doesn't make coordinators hoard hints without bound. Operate it with `nodetool`:

```bash
nodetool statushandoff     # is handoff on?
nodetool disablehandoff    # e.g. before a planned, lengthy node outage
nodetool enablehandoff
nodetool truncatehints      # drop accumulated hints (all, or per-endpoint)
```

A practical gotcha ([The Last Pickle](https://thelastpickle.com/blog/2018/03/21/hinted-handoff-gc-grace-demystified.html)): because a node down past the window won't get its hints, you must run repair within `gc_grace_seconds` or a delete can resurrect — hints are a convenience layer, not a substitute for repair.

## Riak: sloppy by default, strict on demand

Riak makes the same tradeoff configurable per request. It normally writes to primary vnodes "but in case of failure, those operations will go to failover nodes in order to comply with the R and W values" — a sloppy quorum, with the failed vnode's data handed back via hinted handoff on recovery. Riak also lets you *opt out*: setting `pr` (primary read) or `pw` (primary write) above zero "produces a mode of operation called *strict quorum*," which requires responses from the primary vnodes themselves. That disables the fallback — "a higher probability that reads or writes will fail because primary vnodes are unavailable," in exchange for "more likely to receive the most up-to-date values." It's the sloppy/strict dial in two parameters.

| | Strict quorum | Sloppy quorum + hinted handoff |
|---|---|---|
| Quorum membership | first `N` home nodes | first `N` *healthy* nodes on the pref list |
| One home node down | write blocks or fails | write succeeds on a stand-in |
| Overlap guarantee | holds (`R + W > N`) | suspended until hints replay |
| Convergence backstop | — | hint replay, then read-repair / anti-entropy |
| Cassandra / Riak knob | `pr`/`pw` > 0 (Riak) | default; `hinted_handoff_enabled`, `max_hint_window` |

The mental model stays simple: strict quorum optimizes for a consistency proof; sloppy quorum optimizes for the write completing. Hinted handoff is the promise that the two eventually reconcile — bounded by a window, backed by repair.

**Try next:** on a 3-node Cassandra cluster with `RF=3`, `nodetool drain` one node, write a row at `CONSISTENCY QUORUM`, and confirm it still succeeds. Watch a hint file appear under `data/hints/` on a coordinator, restart the drained node, and tail the logs for `HintsService` replay — then verify the row is present on the recovered node.
