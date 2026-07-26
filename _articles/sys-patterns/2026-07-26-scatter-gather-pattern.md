---
title: "Scatter/Gather: Buying Latency With Parallelism"
date: 2026-07-26
track: sys-patterns
summary: "Burns' scatter/gather pattern fans a request out to many leaves in parallel and merges their partial answers to cut latency — but the merge step means your P99 is only as fast as your slowest leaf, and fan-out width has a computational-cost tax."
reading_time: 5
tags: [scatter-gather, latency, fan-out, tail-latency, hedged-requests, distributed-search, burns]
sources:
  - title: "Designing Distributed Systems, 2nd ed. — Ch. 8, Scatter/Gather (Burns, O'Reilly)"
    url: "https://www.oreilly.com/library/view/designing-distributed-systems/9781098156343/ch08.html"
  - title: "The Tail at Scale (Dean & Barroso, Communications of the ACM, 2013)"
    url: "https://cacm.acm.org/research/the-tail-at-scale/"
  - title: "Design Patterns for Container-based Distributed Systems (Burns & Oppenheimer, USENIX HotCloud '16)"
    url: "https://www.usenix.org/conference/hotcloud16/workshop-program/presentation/burns"
  - title: "Scatter–Gather — Distributed Application Architecture Patterns (jurf.github.io)"
    url: "https://jurf.github.io/daap/scalability-patterns/scatter-gather/"
  - title: "Designing Distributed Systems — Scatter-gather & FaaS with event-driven pattern (gemsofcoding.com)"
    url: "https://gemsofcoding.com/Designing-Distributed-Systems-Scatter-Event-Driver/"
---

The [sharded-service pattern](/articles/sys-patterns/2026-07-26-sharded-service-pattern) on this journal solves a *capacity* problem: state too big for one node, so a root routes each request to the *one* shard that owns it. Scatter/gather solves a different problem entirely — it's a *serving* pattern for cutting **latency** on a single request that's too big for one node to compute alone. Burns puts it in the same tree-topology family (a root, a set of leaves) but flips the routing rule: instead of sending a request to the one correct leaf, the root sends it to *every* leaf at once.

## Root and leaves, fired in parallel

The shape: a **root** receives the request, **fans it out** simultaneously to all leaves (or all leaves relevant to the query), each leaf does a bounded slice of work over its own data, and the root **merges** the partial results into one response. Burns frames it as trading *replication for scalability in time* — you're not replicating for redundancy, you're using the replicas' combined CPU to answer one request faster than any single one of them could.

The canonical example is distributed search: an index sharded across a hundred machines, a query that needs to touch all hundred, and a client that can't wait for them sequentially. Scatter the query, let a hundred CPUs work on it in parallel for 20ms each, gather and re-rank the top results. Sequentially that's 2 seconds; in parallel it's roughly the time of the slowest one.

That word "slowest" is the whole story of this pattern's failure modes.

## The merge step is where correctness lives

Fan-out is the easy half. The **gather/merge** step decides what "the response" even means when it's assembled from N independent, partial answers computed against N different slices of data — dedupe, re-rank by a global score, combine partial aggregates (sum, top-k, histogram buckets), and — critically — decide what to do when a leaf didn't answer at all. A merge function that silently assumes all N leaves always respond will produce a response object that looks fine and is quietly wrong the first time a leaf times out.

## Failure mode 1: the straggler tames your average, ruins your tail

Burns calls out that the pattern's latency is bounded by the *slowest* leaf, not the average one — and Dean & Barroso's "The Tail at Scale" (CACM, 2013) is the paper that quantifies why fan-out makes this brutal. Their illustrative model: if a single server has a 99th-percentile latency of 1 second, and a request must fan out to 100 such servers to complete, **63% of requests** end up waiting past that one-second mark — because completing "in time" now requires *all 100* to land inside their own 99th percentile simultaneously. At 2,000 servers it's essentially guaranteed that someone is slow. Their real production numbers make the same point without the model: a single leaf's own P99 is 10ms, but the P99 of the *fanned-out* request — waiting on every leaf — is 140ms. Widening the fan-out to go faster on average makes the tail worse, not better, and the tail is what a user actually experiences on a bad day.

Their fix, and the standard mitigation for this pattern, is **hedged requests**: don't just wait — after a short delay (Dean & Barroso use roughly the 95th-percentile latency), fire a duplicate request to a second replica of the same leaf and take whichever answers first, cancelling the other. Their benchmark cut P99.9 from 1,800ms to 74ms for about 2% more load. **Tied requests** (queue on two replicas simultaneously, cancel cross-server on completion) trade a small resource cost for an even larger cut. Either way, the fix isn't "make every leaf fast" — at scale, someone is always the slow one that day — it's "don't let waiting on any one leaf gate the whole response."

## Failure mode 2: computational cost doesn't stay flat

The second problem Burns highlights is less about latency variance and more about the shape of the cost curve. Adding leaves shrinks the *data* each leaf must scan — that's the entire point — but it does not shrink the fixed overhead of dispatching a request, opening a connection, and merging one more partial result at the root. Fan out to 10 leaves and each does 1/10th the compute; fan out to 1,000 and each is nearly idle while the root pays dispatch-and-merge overhead 1,000 times over and now also has 1,000 chances to hit a straggler or a dead leaf. Wider isn't free — it converts a compute-bound problem into an overhead-and-tail-latency-bound one, and the pattern's sweet spot is the fan-out width where those two costs cross.

| | Sharded service | Work queue | Scatter/gather |
|---|---|---|---|
| Goal | Capacity (data too big for one node) | Throughput (batch of independent items) | Latency (one request too big to compute serially) |
| Root's job | Route to the *one* correct shard | N/A — workers pull for themselves | Fan out to *all* leaves, then merge |
| Dominant failure | Hot shard | Slow/dead worker (retried later) | Straggler leaf (blocks the response *now*) |
| Scaling knob | Shard count vs. data volume | Worker count vs. queue depth | Leaf count vs. dispatch/merge overhead |

## A root that fans out, times out per leaf, and hedges

```python
import asyncio

LEAF_TIMEOUT = 0.150   # hard budget: don't let one leaf gate the response
HEDGE_DELAY = 0.050    # fire a backup replica if the primary is slow

async def call_leaf(session, url, query):
    async with session.get(url, params={"q": query}) as resp:
        return await resp.json()

async def call_leaf_hedged(session, primary_url, backup_url, query):
    """Race a primary replica against a backup fired after HEDGE_DELAY.
    Whoever answers first wins; the loser is cancelled."""
    primary = asyncio.ensure_future(call_leaf(session, primary_url, query))
    done, _ = await asyncio.wait({primary}, timeout=HEDGE_DELAY)
    if primary in done:
        return primary.result()
    backup = asyncio.ensure_future(call_leaf(session, backup_url, query))
    done, pending = await asyncio.wait(
        {primary, backup}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    return done.pop().result()

async def scatter_gather(session, leaves, query):
    async def bounded(leaf):
        try:
            return await asyncio.wait_for(
                call_leaf_hedged(session, leaf["primary"], leaf["backup"], query),
                timeout=LEAF_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return None  # merge must tolerate a missing leaf, not crash on it

    partials = await asyncio.gather(*(bounded(l) for l in leaves))
    return merge(partials, query)

def merge(partials, query):
    hits = [h for p in partials if p is not None for h in p["hits"]]
    hits.sort(key=lambda h: h["score"], reverse=True)
    return {
        "query": query,
        "results": hits[:20],
        "leaves_answered": sum(p is not None for p in partials),
        "leaves_total": len(partials),
    }
```

Three things this snippet insists on: every leaf call has a hard per-leaf `LEAF_TIMEOUT` so one straggler can't block the whole `gather`; the primary/backup race inside `call_leaf_hedged` is the hedged-request mitigation from Dean & Barroso applied at the single-leaf level; and `merge` treats a missing leaf (`None`) as a normal, expected input rather than an exception — it returns `leaves_answered` so callers can tell a complete result from a degraded one instead of silently trusting a partial answer.

**Try next:** instrument the snippet above with per-leaf timing, run it against 10 simulated leaves where one has a long-tail latency distribution (mix of 20ms and, 1% of the time, 2s), and compare P50/P99 of the merged response with `HEDGE_DELAY` on versus set to `None` — that's Dean & Barroso's whole argument, reproduced on your own laptop.
