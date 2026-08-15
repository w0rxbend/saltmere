---
title: "Branch by Abstraction and Splitting the Database: Newman's Incremental Decomposition Toolkit"
date: 2026-08-13
track: microservices
summary: "Carving a capability out of a monolith without a long-lived branch: an abstraction seam, a second implementation grown on trunk, and a flagged switch. Plus the database half — views, wrapping services, split tables, and foreign keys replaced by API calls."
reading_time: 6
tags: [monolith-to-microservices, branch-by-abstraction, database-decomposition, refactoring, trunk-based-development]
sources:
  - title: "Monolith to Microservices — Sam Newman"
    url: "https://samnewman.io/books/monolith-to-microservices/"
  - title: "Branch By Abstraction (Martin Fowler's bliki)"
    url: "https://martinfowler.com/bliki/BranchByAbstraction.html"
  - title: "Branch by Abstraction — Paul Hammant (2007)"
    url: "https://paulhammant.com/blog/branch_by_abstraction.html"
  - title: "Make Large Scale Changes Incrementally with Branch By Abstraction — Jez Humble"
    url: "https://continuousdelivery.com/2011/05/make-large-scale-changes-incrementally-with-branch-by-abstraction/"
---

**Gist.** A capability invoked deep inside a monolith by ordinary method calls cannot be intercepted by an edge proxy, so the strangler fig pattern does not reach it, and extracting it on a long-lived version-control branch defers all integration risk to a single merge. Branch by abstraction — the name comes from Paul Hammant's 2007 write-up — moves the branch into the source code: an interface is introduced over the functionality, both the old and the new implementation live on trunk simultaneously, and a flag selects between them. The cost is a period during which **two implementations of the same behaviour must be kept working at once**, plus a cleanup step that is easy to skip and, when skipped, leaves permanent dead code behind an indirection that no longer earns its place.

## Why not a long-lived branch

The direct plan — branch, remove the subsystem, merge on completion — concentrates every conflict at the end. Jez Humble's objection is that the pain of the merge back grows with the size of the change. While the branch is open, its contents are not exercised by continuous integration against the mainline, so the divergence is unmeasured until the merge attempt.

Branch by abstraction inverts the schedule. Each of the four steps below is a small commit that leaves the system compiling, tested, and releasable, which means integration failures surface **on the commit that caused them** rather than months later.

## The four steps

1. **Create an abstraction** over the functionality to be replaced — typically Extract Interface applied to the existing code.
2. **Refactor all clients** to depend on the abstraction rather than the concrete class. This ships with no behavioural change; the only observable difference is the call target.
3. **Build the second implementation** of the same abstraction. For a microservice extraction it is a client that calls the new service. It lives on trunk, unreferenced by production traffic, growing commit by commit.
4. **Switch** the abstraction to the new implementation, preferably per request behind a feature flag, so that both the switch and its reversal are configuration changes rather than deployments.

Cleanup is a fifth step, and the accounts of the technique include it: delete the old implementation, and delete the abstraction as well if it no longer earns its place. Until it is done, the codebase retains an interface with one implementation and a flag whose false branch is untested.

The invariant that makes the technique safe is that **the abstraction's contract does not change while the second implementation is being built**. If the interface is widened to accommodate the remote implementation, the old implementation must be widened with it, and step 4 stops being a switch between equivalents.

Holding both implementations behind one interface has a further consequence: they can be run in parallel on the same inputs and their results compared before the new one is trusted — the parallel-run verification pattern covered earlier here.

### Implementation sketch (Scala)

```scala
trait LoyaltyPoints:
  def award(customer: CustomerId, total: Money): Unit

// Step 1-2: existing logic, unchanged, now behind the abstraction.
final class LocalLoyaltyPoints(db: Database) extends LoyaltyPoints:
  def award(customer: CustomerId, total: Money): Unit = // ... unchanged
    ()

// Step 3: second implementation, on trunk, not yet selected.
final class RemoteLoyaltyPoints(client: LoyaltyClient) extends LoyaltyPoints:
  def award(customer: CustomerId, total: Money): Unit =
    client.post("/awards", AwardRequest(customer, total))

// Step 4: per-request selection; reversal is a flag write, not a deploy.
final class SwitchingLoyaltyPoints(
    flags: Flags, local: LoyaltyPoints, remote: LoyaltyPoints
) extends LoyaltyPoints:
  def award(customer: CustomerId, total: Money): Unit =
    if flags.enabled("loyalty-remote", customer) then remote.award(customer, total)
    else local.award(customer, total)

// Optional parallel run: the old implementation remains authoritative.
final class ComparingLoyaltyPoints(local: LoyaltyPoints, remote: LoyaltyPoints)
    extends LoyaltyPoints:
  def award(customer: CustomerId, total: Money): Unit =
    local.award(customer, total)
    try remote.award(customer, total)      // side effects land twice; see Pitfalls
    catch case _: Exception => ()          // divergence must not fail the request
```

## The database is the harder half

Code extraction does not make a service independent; a service that still reads and writes the monolith's tables is deployable only in lockstep with it. Newman treats the **shared database** as an antipattern for service boundaries: the schema becomes a de facto public interface, every change requires agreement from every reader, and independent deployability is lost. His decomposition patterns are transitional moves, each chosen so the system stays releasable between steps.

| Pattern | What it does | When |
|---|---|---|
| Database view | Exposes a read-only, reshaped projection to external readers | Cheap first step; readers only, same database engine |
| Database wrapping service | Places a service in front of the schema and bans direct access | Schema too tangled to split yet |
| Split table | One table serving two concerns becomes two tables | Columns or rows owned by different bounded contexts |
| Move foreign key to code | Replaces a cross-boundary foreign key and join with an API call | The relationship crosses the new service boundary |

**Database view.** External readers are pointed at a view instead of the base tables, which restores some freedom to reshape the underlying schema. The limits are structural: the view is read-only, and it generally requires readers to remain in the same database. It is a stepping stone rather than a destination.

**Database wrapping service.** Where the schema resists splitting, a thin service is placed in front of it and direct database access is prohibited. Consumers then depend on an interface under the owning team's control while the underlying tangle is worked on; the tangle itself is contained, not removed.

**Split table.** A `customers` table whose `loyalty_points` column is written by one concern and whose `email` column is written by another becomes two tables. The migration is performed live via expand/contract (parallel change, covered earlier here): add the new table, dual-write, backfill, migrate readers, then drop the old columns. Each of those five states is independently releasable, and the dual-write window is the only one in which two copies of the same fact exist.

**Move foreign-key relationship to code.** This is the sharpest trade in the set. A foreign key from `orders.catalog_item_id` to `catalog_items` cannot be enforced once the two tables are in different databases. The join moves into application code — read the order rows, then call the catalog service for names — and the constraint disappears. The database can no longer prevent a catalog item being deleted while orders still reference it, so the deletion policy becomes an explicit design decision, with options including **forbid deletion, tolerate dangling references in the reading code, or model deletion as a soft state change**. Two properties are lost in the move: the single-round-trip join is replaced by a network call, and the referential guarantee that held at commit time becomes eventual. Newman's position on the remaining case is that if two records genuinely require transactional consistency, they likely belong in the same service — the schema is evidence about where the boundary is not.

## Choosing an entry point

An edge-invoked capability is reachable by strangler fig. A capability invoked internally requires branch by abstraction. Data whose ownership is disputed is contained first with a view or a wrapping service, and split into tables and code-level relationships as the boundaries become clear. Every step in all three is incremental on trunk, releasable, and reversible until the final cleanup, which is the property the long-lived branch does not have.

## Pitfalls

- **The abstraction is widened for the remote implementation.** Symptom: step 4 changes behaviour rather than switching between equivalents, because the local implementation was given a stub for the new method. Cause: the contract was allowed to drift while the second implementation was under construction.
- **Cleanup is deferred indefinitely.** Symptom: an interface with one implementation, and a feature flag whose disabled branch has not executed in production for months and no longer compiles against current code. Cause: step 5 has no user-visible payoff and is omitted from the extraction's definition of done.
- **A parallel run duplicates side effects.** Symptom: loyalty points awarded twice, or two confirmation emails per order. Cause: the comparison harness invoked both implementations, but only read paths are safe to duplicate — a write path needs the non-authoritative implementation to be inert.
- **Dual-write during a split table loses the second write.** Symptom: the new table diverges from the old one for rows updated during the migration window. Cause: the two writes are not in one transaction, so a failure between them leaves the old table updated and the new one stale; backfill must therefore be repeatable and run after dual-write starts, not before.
- **Removing a foreign key without choosing a deletion policy.** Symptom: order pages fail when a catalog item is deleted, having previously been impossible. Cause: the constraint that made the case unreachable is gone, and no code path replaced it.
- **A view is treated as the finished boundary.** Symptom: the extracted service cannot be given its own database because it depends on a view that exists only inside the monolith's engine. Cause: the transitional pattern was mistaken for the destination.
