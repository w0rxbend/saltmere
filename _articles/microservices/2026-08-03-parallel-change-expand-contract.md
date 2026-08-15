---
title: "Parallel change: renaming a column without a flag day"
date: 2026-08-03
track: microservices
summary: "A breaking change to a database column, a JSON field, or a service contract is dangerous because it is atomic while the deployment that carries it is gradual. Parallel change — expand, migrate, contract — splits the change into three separately deployable, separately reversible phases in which the old and new shapes coexist. A concrete column rename in SQL, and the same three phases applied to an API field."
reading_time: 7
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

**Gist.** A rename such as `ALTER TABLE customers RENAME COLUMN name TO full_name` changes the contract atomically, while a rolling deploy changes the code gradually: for the duration of the roll-out, instances of both versions run against a schema that can satisfy only one of them. Parallel change removes the conflict by never performing an atomic break — the new shape is added, both shapes are kept consistent while consumers move, and the old shape is removed only once nothing references it. The cost is that a single migration becomes **a sequence of separately deployed steps**, plus a window in which two representations of the same datum must be kept in agreement.

## Why the rename fails

During a rolling deploy the fleet is mixed-version by construction. Old instances issue `INSERT`/`UPDATE` statements naming `name`; new instances issue statements naming `full_name`. A rename is a single statement with no intermediate state: **before it commits, every reference to `full_name` fails; after it commits, every reference to `name` fails.** There is no ordering of the migration relative to the code roll-out that leaves both halves of the fleet valid, because the schema admits exactly one of the two names at any instant.

The failure mode is therefore not a slow query or a lock — it is a set of statements that raise "column does not exist" on whichever fraction of the fleet is on the losing side of the cutover, for as long as the mixed-version window lasts.

## The three phases

Danilo Sato's **parallel change** pattern (also called *expand and contract*) replaces the atomic break with three phases, each independently deployable and each leaving a system that runs.

- **Expand** — add the new element (column, field, method) *alongside* the old one. Additive only; existing readers and writers are untouched. Reversible on its own.
- **Migrate** — bring both representations into agreement and move consumers: dual-write, backfill history, then switch reads. This is the longest phase; for external application programming interface (API) consumers it can run for weeks.
- **Contract** — once nothing reads or writes the old element, remove it. The breaking change ships here, when it breaks nothing.

The invariant that makes the sequence safe: **no single deploy removes a shape that some running consumer still depends on.** Every phase boundary is crossed while both shapes are valid, so the system is never in a state where a rollback requires a data migration.

The pattern is distinct from the strangler fig, which incrementally replaces a whole system behind a proxy. Parallel change operates one level down, on the shape of a single contract while the system keeps serving.

## A column rename: `name` → `full_name`

Assume `customers.name` and a service deployed as N rolling instances.

**Expand — add the new column, non-destructively.**

```sql
-- Nullable and without a default, so the statement adds metadata
-- rather than rewriting every row.
ALTER TABLE customers ADD COLUMN full_name TEXT NULL;
```

This migration is deployed *ahead of any code change*. The column exists, is empty, and has no readers. It is reversible by `DROP COLUMN` with no consumer impact.

**Migrate, step 1 — dual-write.** Application code is deployed that writes *both* columns on every insert and update, so every row created or modified from this point on carries the two values in agreement. A database trigger is the common alternative when writers are numerous; application-level dual-write keeps the logic in the same repository as its tests.

```sql
INSERT INTO customers (id, name, full_name) VALUES ($1, $2, $2);
UPDATE customers SET name = $2, full_name = $2 WHERE id = $1;
```

Reads still come from `name`, so an error in the new write path corrupts only a column nothing consumes.

**Migrate, step 2 — backfill.** Rows written before the dual-write deploy still have `full_name IS NULL`. They are updated in bounded batches rather than one statement, which keeps each transaction short:

```sql
UPDATE customers
SET full_name = name
WHERE full_name IS NULL
  AND id BETWEEN $lo AND $hi;   -- the range is looped in chunks
```

The backfill is complete when `SELECT count(*) FROM customers WHERE full_name IS NULL` returns 0. **Completion of the backfill is the precondition for switching reads**: until it holds, a read of `full_name` can return null for a row whose `name` is populated.

**Migrate, step 3 — switch reads.** Code that reads `full_name` is deployed while dual-write continues. This is the reversible checkpoint: because both columns remain current, reverting the read to `name` requires no data migration. A feature toggle on the read path allows the read side to be flipped without a redeploy and therefore decouples the two switches in time.

**Contract — drop the old column.**

```sql
-- 1. Stop writing `name` (deploy code that writes only full_name).
-- 2. Then, in a later migration:
ALTER TABLE customers DROP COLUMN name;
```

The ordering is load-bearing: **writes to the old column stop in one deploy, and the `DROP` ships in a subsequent one.** Dropping while any instance of the previous version may still write `name` reproduces the original mixed-version failure. The contract phase is itself a small parallel change.

### Implementation sketch (Scala)

The read side is the only switch that must be flippable at runtime; the write side is fixed per deploy. Modelling the migration as an explicit phase makes the illegal combinations unrepresentable.

```scala
enum Phase:
  case Expand            // write old only; read old
  case DualWrite         // write both;    read old
  case ReadNew           // write both;    read new
  case Contract          // write new only; read new

final case class Customer(id: Long, fullName: String)

trait Rows:
  def upsert(sql: String, args: Seq[Any]): Unit
  def queryOne(sql: String, args: Seq[Any]): Option[String]

final class CustomerRepo(rows: Rows, phase: () => Phase):

  def save(c: Customer): Unit = phase() match
    case Phase.Expand =>
      rows.upsert("UPDATE customers SET name = ? WHERE id = ?", Seq(c.fullName, c.id))
    case Phase.DualWrite | Phase.ReadNew =>
      // Single statement: the two columns cannot diverge on a partial failure.
      rows.upsert("UPDATE customers SET name = ?, full_name = ? WHERE id = ?",
                  Seq(c.fullName, c.fullName, c.id))
    case Phase.Contract =>
      rows.upsert("UPDATE customers SET full_name = ? WHERE id = ?", Seq(c.fullName, c.id))

  def load(id: Long): Option[Customer] =
    val column = phase() match
      case Phase.Expand | Phase.DualWrite => "name"
      case Phase.ReadNew | Phase.Contract => "full_name"
    rows.queryOne(s"SELECT $column FROM customers WHERE id = ?", Seq(id))
         .map(Customer(id, _))
```

`phase` is a function rather than a value so that the toggle can advance without restarting the process. Because the dual write is one statement, no partial failure leaves the two columns disagreeing for a row.

## The same phases on an API field

The pattern applies to a contract, not to a database in particular. Splitting `name` into `firstName` and `lastName` in a JavaScript Object Notation (JSON) payload follows the same sequence.

- **Expand** — emit the new fields in addition to the old one. A tolerant reader, described by Sam Newman in *Building Microservices*, ignores fields it does not recognise, so an additive change breaks no conformant client.

  ```json
  { "id": 42, "name": "Ada Lovelace", "firstName": "Ada", "lastName": "Lovelace" }
  ```

- **Migrate** — consumers move to `firstName`/`lastName`. External clients' schedules are outside the provider's control, so `name` is marked deprecated and its usage is tracked with a per-field log or metric until the count reaches zero.
- **Contract** — with telemetry showing no reads of `name`, the field is removed.

Newman's broader position frames this: prefer to avoid breaking changes at all (add rather than remove; use tolerant readers; catch structural breaks with schemas), and where a break is unavoidable, **let the old and new coexist** instead of scheduling a lockstep release in which every service updates together.

## Pitfalls

- **The contract phase is never executed.** The schema retains both `full_name` and a disused `name`, dual-write code outlives its purpose, and responses accumulate deprecated fields. The cause is structural: expand and migrate deliver visible progress at low risk, while contract delivers none and carries the only irreversible step.
- **Reads are switched before the backfill finishes.** Rows written before the dual-write deploy return null from `full_name`, and the null surfaces as missing data rather than as an error.
- **The old column is dropped in the same deploy that stops writing it.** Instances of the previous version still in the rolling window write to a column that no longer exists — the original failure, reintroduced at the last step.
- **Dual-write is implemented as two statements.** A failure between them leaves the columns disagreeing for that row, and the disagreement is invisible until reads switch.
- **A backfill runs as one `UPDATE`.** The transaction holds locks on every row it touches for its whole duration and must be retried from the beginning if it aborts.
- **Old-field usage is judged by code search rather than telemetry.** Reports, ad-hoc queries and downstream jobs reference the column without appearing in the service's repository, so the contract phase drops a column that still has readers.
