---
title: "Branch by Abstraction and Splitting the Database: Newman's Incremental Decomposition Toolkit"
date: 2026-08-13
track: microservices
summary: "How to carve a capability out of a monolith without a long-lived branch: build an abstraction seam, grow the new implementation behind it on trunk, and flip. Plus the database half of the job — views, wrapping services, split tables, and replacing foreign keys with API calls."
reading_time: 5
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

Strangler fig (covered earlier here) intercepts calls at the *edge* of the monolith. But plenty of functionality you want to extract sits deep inside it — invoked by internal method calls, not HTTP — where no proxy can reach. Sam Newman's *Monolith to Microservices* answer for that case is **branch by abstraction**: a technique named by Paul Hammant in 2007, and the standard way to do a months-long extraction while shipping trunk every day.

## Why not a long-lived branch?

The naive plan — branch, rip out the subsystem, merge when done — fails predictably. Jez Humble's critique: the merge back "is guaranteed to be painful, and the amount of pain is a function of how big the change is." Meanwhile the branch is invisible to CI in any meaningful sense, blocks releases, and can't be abandoned cheaply. Branch by abstraction makes the "branch" in the *source code* instead: two implementations coexist on trunk, and the system builds, tests, and deploys throughout.

## The four steps

1. **Create an abstraction** over the functionality to be replaced — usually just Extract Interface over the existing code.
2. **Refactor all clients** to call the abstraction instead of the concrete code. Ship it; nothing has changed behaviorally.
3. **Build the new implementation** of the same abstraction — for microservice extraction, a thin client calling the new service. It lives on trunk, dark, growing commit by commit.
4. **Switch** the abstraction to the new implementation — ideally per-request via a feature flag, so the switch is a config change with instant rollback.

Then clean up: delete the old implementation, and delete the abstraction too if it no longer earns its keep. (Fowler and Hammant both list the cleanup as an explicit final step.)

The seam in code:

```java
// BEFORE: callers reach directly into monolith internals
class OrderWorkflow {
  void complete(Order o) {
    new LoyaltyPointsCalculator(db).award(o.customerId(), o.total());
  }
}

// STEP 1-2: abstraction, existing logic behind it
interface LoyaltyPoints { void award(CustomerId c, Money total); }
class LocalLoyaltyPoints implements LoyaltyPoints { /* old code, unchanged */ }

// STEP 3: new implementation calls the extracted service
class RemoteLoyaltyPoints implements LoyaltyPoints {
  void award(CustomerId c, Money total) {
    loyaltyClient.post("/awards", new AwardRequest(c, total));
  }
}

// STEP 4: switch behind a flag
LoyaltyPoints points = flags.enabled("loyalty-remote")
    ? new RemoteLoyaltyPoints(client) : new LocalLoyaltyPoints(db);
```

Newman points out a bonus: once both implementations sit behind one interface you can run them *in parallel* and compare results before trusting the new one — the parallel-run verification pattern covered earlier here.

## The database is the hard half

Code extraction is the warm-up; the service isn't independent until it owns its data. Newman is blunt that the **shared database** is an antipattern for service boundaries: schemas become a public API, any change requires coordinating every reader, and independent deployability — the whole point — dies. His decomposition patterns are transition moves, each keeping the system releasable:

| Pattern | What it does | When |
|---|---|---|
| Database view | Expose a read-only, reshaped projection to external readers | Cheap first step; readers only, same DB engine |
| Database wrapping service | Put a service in front of the schema; ban direct access | Schema too tangled to split yet; stops the bleeding |
| Split table | One table serving two concerns becomes two tables | Columns (or rows) are owned by different bounded contexts |
| Move foreign-key to code | Replace cross-boundary FK + join with an API call | The relationship crosses the new service boundary |

**Database view**: external readers get a view instead of raw tables. You've regained some freedom to change the underlying schema, but it's read-only and typically requires living in one database — a stepping stone, not a destination.

**Database wrapping service**: when the schema is too entangled to split soon, front it with a thin service and make direct DB access a firing offense. Consumers now depend on an API you control; the mess is contained while you work.

**Split table**: a `customers` table where `loyalty_points` is updated by one concern and `email` by another becomes two tables — done live via expand/contract (parallel change, covered earlier here): add the new table, dual-write, backfill, migrate readers, drop the old columns.

**Move foreign-key relationship to code**: the sharpest trade. An FK from `orders.catalog_item_id` to `catalog_items` can't survive the tables landing in different databases. The join moves into code (fetch order rows, then call the catalog service for names), and the constraint disappears entirely — the database can no longer stop a catalog item being deleted while orders reference it. You must now choose explicitly: forbid deletion, tolerate dangling references gracefully, or model deletion as soft. Expect latency where a join used to be, and eventual consistency where an FK used to be. If two things must genuinely be transactionally consistent, Newman's advice is that they probably belong in the *same* service — the schema is telling you where the boundary isn't.

## Choosing your entry point

Edge-invoked capability: strangler fig. Deep internal capability: branch by abstraction. Data no one can agree on: start with a view or wrapping service to contain it, then split tables and unwind foreign keys as real boundaries emerge. Every one of these is incremental on trunk, releasable at each step, and reversible until the final cleanup — which is precisely why they beat the big-bang rewrite branch.

**Try next:** Pick one subsystem in a codebase you know and do steps 1–2 only — extract the interface and route all callers through it; the diff is small, ships safely today, and tells you exactly how tangled step 3 will be.
