---
title: "Parallel change: rename a column without a flag day"
date: 2026-08-03
track: microservices
summary: "Every breaking change to a database column, a JSON field, or a service contract is dangerous only because you try to do it all at once. Parallel change — expand, migrate, contract — splits the change into three separately deployable, separately reversible phases so old and new coexist while you move consumers over. Here's a concrete column rename with SQL, and the same three phases applied to an API field."
reading_time: 6
tags: [migrations, schema-evolution, parallel-change, expand-contract, zero-downtime, backward-compatibility]
sources:
  - title: "ParallelChange — Danilo Sato (martinfowler.com)"
    url: "https://martinfowler.com/bliki/ParallelChange.html"
  - title: "Building Microservices, 2nd Edition — Sam Newman"
    url: "https://samnewman.io/books/building_microservices_2nd_edition/"
  - title: "Expand and Contract Method for Database Changes — Jasmin Fluri"
    url: "https://medium.com/@jasminfluri/expand-and-contract-method-for-database-changes-414d236f236f"
  - title: "Expand-Contract Pattern for Database Changes — CI/CD for Software, Data, and Infrastructure"
    url: "https://cicd.ariefw.com/chapters/chapter-21/"
---

Renaming a column looks like a one-liner: `ALTER TABLE customers RENAME COLUMN name TO full_name`. Run it in production against a service that has more than one running instance, though, and you have engineered an outage. During the rolling deploy, old instances are still writing to `name` while new instances read `full_name`; the moment the `ALTER` lands, half your fleet is querying a column that no longer exists. The rename is *atomic and irreversible* at exactly the moment your deployment is *gradual and mixed-version*. Those two facts cannot both be true safely.

Danilo Sato's **parallel change** pattern (he also calls it *expand and contract*) dissolves the conflict by refusing to make the breaking change atomic at all. You split it into three phases — **expand**, **migrate**, **contract** — each of which is independently deployable and, crucially, leaves the system in a working state you can stop at or roll back from. Sato's key line is that the code "can be released in any of these three phases." The old and new shapes coexist on purpose, and the breaking part only happens at the very end, after nothing depends on the old shape anymore.

This is the disciplined version of the advice Sam Newman gives in *Building Microservices*: **prefer to avoid breaking changes** ("just add, never remove"; use a tolerant reader; catch structural breaks with schemas), and when you genuinely must break something, **coexist old and new** rather than force a lockstep deployment where every service updates in the same release. Newman treats simultaneous "flag day" deploys as the thing to design your way out of. Parallel change is how.

## The three phases

- **Expand** — Add the new element (column, field, method) *alongside* the old one. Additive only. Old readers and writers are untouched. Deployable and reversible on its own.
- **Migrate** — Make both sides consistent and move consumers over: dual-write, backfill historical data, then switch reads to the new element. This is the longest phase, and for external API consumers it can last weeks.
- **Contract** — Once nothing reads or writes the old element, remove it. Only now does the "breaking" change actually happen — and by now it breaks nothing.

The property that makes this safe: at no single deploy does a running consumer lose the shape it currently depends on. Contrast with strangler fig, which incrementally *replaces a whole system* behind a proxy; parallel change operates one level down, on the *shape of a single contract* while the system keeps running.

## A concrete column rename: `name` → `full_name`

Assume `customers.name` and a service deployed as N rolling instances. Walk it phase by phase.

**Expand — add the new column, non-destructively.**

```sql
-- Nullable, no default rewrite of the whole table, no lock held long.
ALTER TABLE customers ADD COLUMN full_name TEXT NULL;
```

Deploy this migration *ahead of any code change*. The column exists, is empty, and nobody uses it. Fully reversible (`DROP COLUMN`) with zero consumer impact.

**Migrate, step 1 — dual-write.** Deploy application code that writes *both* columns on every insert and update. Now every new or modified row keeps `name` and `full_name` in sync. A DB trigger is a common alternative when many writers exist, but application-level dual-write keeps the logic visible and testable:

```sql
INSERT INTO customers (id, name, full_name) VALUES ($1, $2, $2);
UPDATE customers SET name = $2, full_name = $2 WHERE id = $1;
```

At this point reads still come from `name` — so if the new column logic is wrong, you have broken nothing.

**Migrate, step 2 — backfill.** New rows are covered; old rows still have `full_name IS NULL`. Backfill them in batches to avoid a table-wide lock or a giant transaction:

```sql
UPDATE customers
SET full_name = name
WHERE full_name IS NULL
  AND id BETWEEN $lo AND $hi;   -- loop the range in chunks
```

When `SELECT count(*) FROM customers WHERE full_name IS NULL` returns 0, the two columns are provably identical.

**Migrate, step 3 — switch reads.** Deploy code that reads from `full_name`. Keep dual-writing for now — this is the reversible checkpoint. If something misbehaves, roll the read back to `name` without a data migration, because both columns are still current. Guarding the read switch behind a feature flag lets you flip it without a redeploy, exactly as Sato suggests for decoupling the two sides.

**Contract — drop the old column.** After the read switch has been stable in production and you've confirmed no query, report, or downstream job still references `name`:

```sql
-- 1. Stop writing `name` (deploy code that writes only full_name).
-- 2. Then, in a later migration:
ALTER TABLE customers DROP COLUMN name;
```

Note the ordering: stop *writing* the old column in one deploy, and only `DROP` it in a *subsequent* one. Dropping while an old instance might still write to `name` reintroduces the exact race you started out avoiding. The contract phase is itself a small parallel change.

That is five to seven separate, individually reversible deploys to accomplish what `RENAME COLUMN` promised in one. The cost is real; the payoff is that no step has an outage or a hard rollback in it.

## The same three phases on an API field

The pattern is contract-shaped, not database-shaped, so it applies unchanged to a service response. Say you're splitting `name` into `firstName` and `lastName` in a JSON payload.

- **Expand** — Emit the new fields *in addition to* the old one. The response now carries `name`, `firstName`, and `lastName`. Because you only added fields, a tolerant-reader client (Newman's recommendation) ignores what it doesn't recognize, and nothing breaks.

  ```json
  { "id": 42, "name": "Ada Lovelace", "firstName": "Ada", "lastName": "Lovelace" }
  ```

- **Migrate** — Update consumers to read `firstName`/`lastName`. You don't own external clients' schedules, so publish a deprecation notice on `name` and track its usage (log or metric per field) until it drops to zero. This is where the phase legitimately runs long.
- **Contract** — Once telemetry shows no consumer reads `name`, remove it from the response. Now — and only now — the breaking change ships, breaking nobody.

## Where it goes wrong

Sato's own caveat is the one to internalize: the danger of parallel change is **never finishing the contract phase**. Half-migrated schemas with a `full_name` *and* a zombie `name`, dual-write code that outlives its purpose, and API responses fat with deprecated fields are worse than the original — you now carry the complexity of two shapes permanently. Expand and migrate are the fun, low-risk phases; contract is the disciplined one, and it's the one teams skip. Make the contract phase a tracked ticket with an owner and a "last usage observed" metric as its exit criterion, or parallel change quietly degrades into permanent duplication.

**Try next:** pick one nullable-column rename in a service you own, write the expand migration and the dual-write code as two separate PRs, and add a metric that counts reads of the old field — you'll have the exact signal that tells you when the contract phase is safe.
