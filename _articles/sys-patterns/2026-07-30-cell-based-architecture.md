---
title: "Cell-Based Architecture: Sharding Failures, Not Only Load"
date: 2026-07-30
track: sys-patterns
summary: "A cell is a complete, isolated copy of a stack; a thin router maps each partition key to exactly one cell. The payoff is not spreading load but capping blast radius, so one bad deploy, poison request, or gray availability-zone failure affects 1/N of users rather than all of them."
reading_time: 6
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

**Gist.** A failure in a shared stack — a bad deploy, a request that crashes its handler, a degraded availability zone (AZ) — propagates to every user of that stack. Cell-based architecture replicates the *whole* stack into N independent copies and routes each partition key to exactly one of them, so the failure domain of any single fault is one cell. The cost is duplicated infrastructure, a hard prohibition on cross-cell coupling, and the need to migrate tenants between cells without losing writes.

## Cells are not shards

The [sharded service](/articles/sys-patterns/2026-07-26-sharded-service-pattern) splits *state* across nodes so more of it fits. Cell-based architecture splits the *entire stack* — load balancer, compute, database, queues, caches — into complete, self-sufficient copies. Both diagrams show a router, a partition key and N buckets behind, but the optimisation targets differ: sharding pursues capacity, cells pursue a bounded failure.

AWS defines a cell as an instance of the complete workload, with everything needed to operate independently, and the isolation rule follows directly: cells "should have no dependency on each other at all (that is, no cross-cell API calls, no shared resources like databases or S3 buckets)". **The invariant is the absence of shared state and shared control paths, not the presence of a partition key.** A shard that loses its node darkens a slice of the keyspace while still sharing a control plane, a deployment pipeline and a database engine with every other shard; a correlated fault in any of those affects all shards at once. A cell shares none of them.

## The router is the single shared component

Every cell architecture contains exactly one component that spans cells: the router mapping a partition key to a cell identifier. Its failure is by construction not contained, so its blast radius is the whole system. AWS's guidance is correspondingly minimal — keep the router "as simple and horizontally scalable as possible", "avoid complex business logic within this layer", and prefer a "computationally efficient" mapping such as "combining cryptographic hash functions and modular arithmetic".

A hash-and-modulus router is stateless and cheap to test, but changing N moves most keys, which makes tenant migration a global event. The common alternative keeps an **explicit placement table consulted before the hash**, so an individual key can be reassigned without disturbing its neighbours. The hash then serves only as the default for keys with no explicit entry.

The router's contract is one function: key in, cell identifier out. No feature flags, no per-tenant business rules, no synchronous call to a downstream service that can itself fail. A router that cannot be read and tested in isolation becomes the correlated failure the cells were built to prevent.

### Implementation sketch (Scala)

```scala
final case class CellId(value: String)

trait CellRouter:
  def cellFor(partitionKey: String): CellId

/** Explicit placements win; unplaced keys fall back to a stable hash.
  * `placement` is a snapshot: the router never blocks on a lookup service. */
final class TableThenHashRouter(
    cells: IndexedSeq[CellId],
    placement: Map[String, CellId]
) extends CellRouter:

  require(cells.nonEmpty, "at least one cell")

  def cellFor(partitionKey: String): CellId =
    placement.getOrElse(partitionKey, hashed(partitionKey))

  private def hashed(key: String): CellId =
    val digest = java.security.MessageDigest
      .getInstance("SHA-256")
      .digest(key.getBytes(java.nio.charset.StandardCharsets.UTF_8))
    // Top 63 bits, sign cleared, so the modulus stays non-negative.
    val bits = java.nio.ByteBuffer.wrap(digest, 0, 8).getLong & Long.MaxValue
    cells((bits % cells.length).toInt)
```

A migration is then a two-phase change: add the tenant's key to `placement` pointing at the new cell only after its data is present there, and keep the old cell readable until reconciliation completes.

## Sizing, and the arithmetic of N

A cell has a **maximum size** — a deliberate cap on how much of the workload one cell may hold, expressed in whatever dimension dominates scaling (tenants, requests per second, stored bytes). The cap exists because the cell is the unit of blast radius: with N equal cells, a single-cell failure affects at most **1/N** of the workload. Ten cells cap impact at 10%, forty cells at 2.5%. The cap also bounds operational risk, since deployments, patches and load tests proceed one cell at a time with observation between steps.

The counter-pressure is cost. Each cell carries its own database, its own capacity headroom and its own monitoring, so fixed per-cell overhead scales with N while the reduction in blast radius scales only as 1/N — the marginal benefit of each additional cell shrinks while its marginal cost does not. Published deployments sit at very different points. Slack maps cells to **availability zones**, following an incident in which one AZ's degraded network cascaded beyond that AZ; the resulting design treats each service as a set of per-AZ siloed copies, so a sick AZ can be drained. Roblox runs coarser cells of roughly **1,400 machines each** and reported tens of cells live at the time of writing, with the fleet migration still in progress.

## Shuffle sharding reduces overlap between tenants

Plain cells give every tenant a 1/N blast radius, but a tenant emitting poison traffic still degrades its entire cell, and therefore every tenant assigned to that cell. **Shuffle sharding** assigns each tenant not to one cell but to a random *subset* of nodes, and relies on the number of distinct subsets. With 8 workers and 2 assigned per tenant there are C(8,2) = 28 distinct pairs; a noisy tenant still degrades both of its own workers, but only the tenants drawn onto that exact pair — roughly 1 in 28 — lose every worker they have. Regular sharding of the same 8 workers into 4 fixed pairs would take out a quarter of the tenants instead.

The combinatorics grow steeply with subset size. Route 53 fronts each customer domain with 4 of 2,048 virtual name servers, giving C(2048,4) ≈ **730 billion** possible shards, so two customer domains are overwhelmingly unlikely to be assigned the same four name servers. **The isolation is only realised if clients retry across the other members of their assigned subset**: without that retry, a request that lands on the affected node fails regardless of how little the subsets overlap.

## Pitfalls

- **A shared table reintroduces the correlated failure.** One "temporary" cross-cell datastore means a single lock, migration or outage in it stalls every cell at once, and the 1/N bound no longer holds even though the topology still looks cellular.
- **Cross-tenant operations sit above the cells.** Global search, organisation-wide reports and tenant merges require a scatter-gather layer that touches every cell, so its availability is the product of the cells' availabilities; placing it on the synchronous request path makes the system less available than a monolith.
- **Retrofitting migration is expensive.** A design that assumed keys never move has no double-write path and no reconciliation, so evacuating a failing cell has to be built during the incident.
- **Business logic in the router.** Per-tenant rules or a synchronous lookup call inside the routing layer put a whole-system dependency into the one component whose failure is uncontained.
- **Modulus-based placement makes rescaling a global event.** Changing N in a hash-and-modulus router relocates most keys simultaneously, which is the opposite of the one-cell-at-a-time operating model the pattern is built around.
- **Deployments that skip the per-cell gate.** Rolling a change to all cells in one pipeline stage restores the original blast radius; the cap on impact comes from the staging discipline, not from the topology alone.
