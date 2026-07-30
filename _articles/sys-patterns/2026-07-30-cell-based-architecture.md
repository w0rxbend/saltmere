---
title: "Cell-Based Architecture: Sharding Failures, Not Just Load"
date: 2026-07-30
track: sys-patterns
summary: "A cell is a complete, isolated copy of your stack; a thin router maps each partition key to exactly one cell. The payoff isn't spreading load — it's capping blast radius so one bad deploy, poison request, or gray AZ failure takes down 1/N of users instead of all of them."
reading_time: 5
tags: [cell-based, blast-radius, shuffle-sharding, fault-isolation, partitioning, bulkheads, aws]
sources:
  - title: "Reducing the Scope of Impact with Cell-Based Architecture (AWS Well-Architected)"
    url: "https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/reducing-scope-of-impact-with-cell-based-architecture.html"
  - title: "Cell routing — Reducing the Scope of Impact with Cell-Based Architecture (AWS)"
    url: "https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-routing.html"
  - title: "Workload isolation using shuffle-sharding (Colm MacCárthaigh, Amazon Builders' Library)"
    url: "https://aws.amazon.com/builders-library/workload-isolation-using-shuffle-sharding/"
  - title: "Slack's Migration to a Cellular Architecture (Slack Engineering)"
    url: "https://slack.engineering/slacks-migration-to-a-cellular-architecture/"
  - title: "How We're Making Roblox's Infrastructure More Efficient and Resilient (Roblox)"
    url: "https://about.roblox.com/newsroom/2023/12/making-robloxs-infrastructure-efficient-resilient"
---

The [sharded service](/articles/sys-patterns/2026-07-26-sharded-service-pattern) splits *state* across nodes so you can hold more of it. Cell-based architecture splits the *entire stack* — load balancer, compute, database, queues, caches — into complete, self-sufficient copies so that when something breaks, it breaks in only one of them. The two look similar on a whiteboard (a router in front, a partition key, N buckets behind), but they optimize for opposite things. Sharding chases capacity. Cells chase a bounded, predictable failure.

AWS states the goal plainly: a cell is "an instance of your complete workload, with everything needed to operate independently." The design rule that follows is the whole pattern in one line — cells "should have no dependency on each other at all (that is, no cross-cell API calls, no shared resources like databases or S3 buckets)." A shard that loses its node darkens a slice of the keyspace but still shares a control plane, a deploy pipeline, and a database engine with every other shard. A cell shares none of that. That is what turns "a slice went dark" into "a slice went dark *and nothing else could*."

## The router is the one shared thing, so keep it dumb

Every cell architecture has exactly one component that spans cells: the router that maps a partition key to a cell. It is also the one component whose failure isn't contained — so its blast radius is the whole system. AWS's guidance is therefore almost aggressively minimalist: keep the router "as simple and horizontally scalable as possible," "avoid complex business logic within this layer," and prefer a "computationally efficient" mapping such as "combining cryptographic hash functions and modular arithmetic."

A hash-mod router is stateless and trivially testable, but it makes migration painful (change `N` and most keys move). Most large deployments instead keep an explicit lookup table, so any key can be reassigned without moving its neighbors:

```python
CELLS = ["cell-a", "cell-b", "cell-c", "cell-d"]

# Explicit overrides win; everything else falls to a stable hash.
PLACEMENT = {}  # partition_key -> cell_id, e.g. after a migration

def cell_for(partition_key: str) -> str:
    if partition_key in PLACEMENT:
        return PLACEMENT[partition_key]          # pinned / migrated keys
    digest = hashlib.sha256(partition_key.encode()).digest()
    return CELLS[int.from_bytes(digest[:8], "big") % len(CELLS)]
```

The router does one thing: key in, cell id out. No feature flags, no per-tenant business rules, no calls to a downstream service that could itself fail. If you cannot understand and test the router in an afternoon, it will eventually become the correlated failure that cells were supposed to prevent.

## Sizing cells, and why more of them is better

A cell has a **maximum size** — a deliberate cap on how much of the workload one cell may hold, expressed in whatever dominates your scaling (tenants, requests/sec, storage). The cap exists because the cell is your unit of blast radius: with `N` equal cells, a single-cell failure affects at most `1/N` of the workload. Ten cells cap impact at 10%; forty cells cap it at 2.5%. The cap also bounds risk during operations — you deploy, patch, or run load tests one cell at a time and watch it before touching the next.

The tension is cost and operational overhead: every cell needs its own database, its own capacity headroom, its own monitoring. Too few large cells and a failure is expensive; too many tiny cells and you drown in fixed per-cell cost and coordination. Real systems land at very different points. Slack maps cells to **availability zones** — after a 2021 gray failure where one AZ's degraded network link cascaded across their monolith, they built "N virtual services, one per AZ" so a sick AZ can be drained. Roblox runs coarser cells of roughly **1,400 machines each**, describing them as "strong blast walls" and had ~21 cells live across a fraction of their fleet at the time of writing.

## Shuffle sharding: overlap is the enemy

Plain cells give every tenant a `1/N` blast radius, but a *specific* tenant sending poison traffic still takes down its whole cell — and everyone sharing it. **Shuffle sharding** shrinks the collateral by assigning each tenant not to one cell but to a random *subset* of nodes, and leaning on combinatorics. With 8 workers and 2 assigned per tenant there are C(8,2) = 28 distinct pairs. Assign tenants to random pairs and a single noisy tenant degrades just "1/28th" of the fleet — the Builders' Library calls this "7 times better than regular sharding." The math compounds fast: Route 53 fronts each customer domain with 4 of 2,048 virtual name servers, giving C(2048,4) ≈ **730 billion** possible shards, so "no customer domain will ever share more than two virtual name servers with any other." Two tenants almost never fully overlap, so one tenant's bad day is invisible to nearly everyone else — provided your clients retry across their assigned subset.

## The parts that hurt

- **Data placement.** A partition key must resolve to exactly one cell's datastore, and that mapping has to survive as authoritative. Cross-cell joins and "just this one shared table" are how a cell architecture quietly decays back into a monolith with extra latency.
- **Cross-cell operations.** Anything spanning tenants in different cells — global search, org-wide reports, a tenant merge — needs a scatter-gather layer *above* the cells, which reintroduces a shared failure domain. Push these to asynchronous, best-effort paths; keep them off the request hot path.
- **Migration.** Moving a tenant between cells (rebalancing, or evacuating a failing cell) means relocating its data and flipping the router entry without dropping writes. Budget for double-writes, a cutover, and reconciliation from day one — retrofitting live migration onto cells that assumed keys never move is brutal.

Cells are not a scaling trick you reach for when a table gets big — that's [sharding](/articles/sys-patterns/2026-07-26-sharded-service-pattern), and it lives one layer down inside each cell. Cells are an availability decision: you accept duplicated infrastructure and a hard rule against cross-cell coupling, and in return every failure you haven't imagined yet is capped at `1/N` before it starts.

**Try next:** Pick one service and compute its real blast radius today — what fraction of users does a single bad deploy or poison request take down? Then sketch the cell count that would cap it at 5%, and identify the one shared dependency (usually the database) that makes true cell isolation hard.
