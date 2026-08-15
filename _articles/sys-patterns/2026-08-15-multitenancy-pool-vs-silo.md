---
title: "Multi-Tenancy Patterns: Pool, Silo, and the Bridge Between"
date: 2026-08-15
track: sys-patterns
summary: "AWS's Well-Architected SaaS Lens names three tenancy models: silo (dedicated stack per tenant), pool (shared everything plus a tenant_id column), and bridge (shared compute, per-tenant database or schema). The substance is in the trade-offs — noisy neighbours and blast radius under pool, cost and operational sprawl under silo — and in the enforcement mechanics: PostgreSQL row-level security driven by current_setting, tenant-aware token buckets, and tiering that pools free tenants while siloing the contracts that require it. Shopify's pods architecture applies the same partitioning at platform scale."
reading_time: 7
tags: [multi-tenancy, saas, silo-pool-bridge, row-level-security, postgres, noisy-neighbor, cell-based]
sources:
  - title: "AWS Well-Architected SaaS Lens — Silo, Pool, and Bridge Models"
    url: "https://docs.aws.amazon.com/wellarchitected/latest/saas-lens/silo-pool-and-bridge-models.html"
  - title: "AWS Whitepaper — SaaS Tenant Isolation Strategies: Pool Isolation"
    url: "https://docs.aws.amazon.com/whitepapers/latest/saas-tenant-isolation-strategies/pool-isolation.html"
  - title: "PostgreSQL Documentation — Row Security Policies"
    url: "https://www.postgresql.org/docs/current/ddl-rowsecurity.html"
  - title: "Shopify Engineering — A Pods Architecture to Allow Shopify to Scale"
    url: "https://shopify.engineering/a-pods-architecture-to-allow-shopify-to-scale"
---

**Gist.** A software-as-a-service (SaaS) platform must decide whether each customer receives dedicated infrastructure or shares one fleet distinguished by a `tenant_id` column. AWS's Well-Architected **SaaS Lens** names the three points on that axis — **silo**, **pool**, and **bridge** — and each buys isolation with a different currency: silo pays in idle capacity and per-tenant operations, pool pays by demoting isolation from a structural property to a software invariant that every query must uphold, and bridge pays in migration fan-out and connection-pool multiplication.

## The three models

**Silo** assigns dedicated infrastructure per tenant — at the extreme a full stack: virtual private cloud (VPC), compute, and database. Isolation is structural: a runaway query issued by tenant A cannot consume tenant B's buffer cache or connection slots, because there is no shared instance to contend for. Per-tenant cost is directly attributable, since it is the bill for that tenant's resources. The costs are the mirror image: idle headroom is provisioned once per tenant rather than once per fleet, deployments and configuration changes scale as **O(tenants)**, configuration drift accumulates between independently mutated stacks, and onboarding requires provisioning infrastructure rather than inserting a row.

**Pool** shares one fleet and one database, with every tenant-scoped table carrying `tenant_id`. Utilisation is high, there is a single deployable, and onboarding is one insert. The compensating cost is that **isolation becomes an application-level invariant**: every read and write must be tenant-scoped, one tenant's traffic surge consumes capacity that all tenants draw from (**noisy neighbour**), and a defective deploy or a dropped index degrades every tenant simultaneously — the maximal **blast radius**.

**Bridge** is the hybrid the SaaS Lens describes: a shared application tier over per-tenant data, realised either as a database per tenant or a schema per tenant within one cluster. One fleet is deployed, while data-level properties come structurally — per-tenant backup and restore ("restore Acme alone to 14:00 yesterday" is a per-database operation under bridge and a row-level reconstruction under pool), and per-tenant encryption keys. The costs are connection-pool fan-out (a per-schema pool multiplied by tenant count), schema migrations executed once per tenant, and drift between tenants whose migrations partially failed.

| | Silo | Bridge | Pool |
|---|---|---|---|
| Isolation | Structural | Structural for data, shared compute | Software-enforced |
| Noisy neighbour | None | Compute-level only | Full exposure |
| Cost per tenant | Highest, but attributable | Medium | Lowest, hard to attribute |
| Onboarding | Provision infrastructure | Create database or schema | Insert a row |
| Operational burden | O(tenants) deploys and configs | One fleet, O(tenants) migrations | One of everything |
| Per-tenant restore | Direct | Direct | Row-level reconstruction |

## Enforcing pool isolation beneath the application

The characteristic pool failure is a single query missing `WHERE tenant_id = ?`, which returns another tenant's rows. AWS's tenant-isolation whitepaper argues for enforcing pooled isolation *below* application code rather than by a coding convention above it. In PostgreSQL that layer is **row-level security (RLS)**: a policy attached to the table whose predicate the server adds to every statement against it, independent of what the query text contains.

```sql
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;  -- applies to the table owner too

CREATE POLICY tenant_isolation ON documents
  USING (tenant_id = current_setting('app.current_tenant')::uuid);

-- per request, after the tenant has been authenticated:
BEGIN;
SET LOCAL app.current_tenant = 'b7f2…';   -- reverts at COMMIT or ROLLBACK
SELECT * FROM documents WHERE title ILIKE '%q3%';  -- planner adds the tenant filter
```

The invariant is: **no statement can observe a row whose `tenant_id` differs from the session variable in force**. Two properties of the mechanism carry the weight. First, `SET LOCAL` binds the setting to the enclosing transaction and reverts at commit or rollback; a plain `SET` persists on the connection, which is unsound when a pooler hands the same physical connection to a different tenant's next request. Second, the PostgreSQL documentation records two ways the policy is skipped entirely: **superusers and roles with the `BYPASSRLS` attribute are exempt from all policies**, and **the table owner is exempt unless `FORCE ROW LEVEL SECURITY` is set**. The application role must therefore be an ordinary role that owns nothing and holds neither attribute. The runtime cost is an additional predicate per query; the guarantee obtained is that a forgotten filter yields zero rows rather than another tenant's rows.

Compute-side, pooled tenants require **tenant-aware admission control**: a token bucket keyed by tenant rather than by user or source address, with refill rates set per tier, and fair queueing in background workers so that one tenant's large export does not occupy every worker slot. The mechanics are the standard [rate-limiting toolkit](/articles/microservices/2026-07-31-rate-limiting-load-shedding-token-bucket) applied at tenant granularity; [shuffle sharding](/articles/sys-patterns/2026-07-31-shuffle-sharding) is the complementary technique that bounds how many other tenants a single misbehaving tenant can affect.

### Implementation sketch (Scala)

The load-bearing idea is that tenancy is a routing decision resolved before any data access, and that the same tenant identifier keys both the connection target and the admission bucket.

```scala
enum Placement:
  case Pool                                  // shared schema, RLS-enforced
  case Bridge(schema: String)                // shared cluster, own schema
  case Silo(cluster: String)                 // own cluster

final case class Tenant(id: java.util.UUID, placement: Placement, refillPerSec: Double)

/** Token bucket keyed by tenant; refill is computed lazily from elapsed time.
  * The read-modify-write below is not atomic — a production bucket needs a
  * `replace` retry loop or a per-tenant lock. */
final class TenantBuckets(capacity: Double):
  private val state = scala.collection.concurrent.TrieMap.empty[java.util.UUID, (Double, Long)]

  def tryAcquire(t: Tenant, now: Long = System.nanoTime()): Boolean =
    val (tok, last) = state.getOrElse(t.id, (capacity, now))
    val refilled = math.min(capacity, tok + (now - last) / 1e9 * t.refillPerSec)
    val granted = refilled >= 1.0
    state.update(t.id, (if granted then refilled - 1.0 else refilled, now))
    granted

/** The tenant is bound for the transaction's lifetime, never for the connection's. */
def withTenant[A](c: java.sql.Connection, t: Tenant)(body: java.sql.Connection => A): A =
  c.setAutoCommit(false)
  try
    t.placement match
      // set_config(..., is_local = true) is the parameterisable form of SET LOCAL
      case Placement.Pool =>
        val ps = c.prepareStatement("SELECT set_config('app.current_tenant', ?, true)")
        ps.setString(1, t.id.toString); ps.execute()
      case Placement.Bridge(schema) =>
        val ps = c.prepareStatement("SELECT set_config('search_path', ?, true)")
        ps.setString(1, schema); ps.execute()
      case Placement.Silo(_) => ()          // connection already targets the tenant's cluster
    val a = body(c); c.commit(); a
  catch case e: Throwable => c.rollback(); throw e
```

## Tiering and composition

The models compose per *tier*: self-serve tenants are pooled, because pooled economics are what make a low price point viable, while contracts carrying compliance and data-residency obligations are placed on bridge or silo. The premium tier sells reduced blast radius as a product feature. The models also compose per *component*: the stateless application tier can be pooled for all tenants while only storage is siloed, since storage is where most isolation requirements are expressed.

## Pods, cells, and migration between models

At platform scale the silo unit stops being a tenant and becomes a group. Shopify's **pods architecture** partitions the platform into pods, each an isolated slice with its own MySQL, Redis, and workers, owning a fixed subset of shops. A pod failure strands only the shops assigned to it; capacity grows by adding pods; high-traffic shops are isolated through placement. This is [cell-based architecture](/articles/sys-patterns/2026-07-30-cell-based-architecture) with tenancy as the partition key — internally a pool, externally a set of silos with bounded blast radius.

The same framing makes **migration between models** a routine operation rather than a rebuild: a pooled tenant that grows into a noisy neighbour is extracted into its own cell or silo. The sequence is to replicate the tenant's rows by dual write or change-data capture, filtered on `tenant_id` — a reason to retain the column even under bridge — verify row counts and checksums, flip the tenant's entry in the tenant catalogue, then delete the pooled copy. The **tenant catalogue**, mapping tenant to model, cell, database, and tier, is the component that keeps the choice reversible: placement stays an implementation detail behind a stable routing layer.

## Pitfalls

- **A plain `SET app.current_tenant` instead of `SET LOCAL`.** The setting survives the transaction and remains on the pooled connection; the next request served by that connection reads the previous tenant's rows, and the leak is invisible until traffic mixes tenants on one connection.
- **The application connects as the table's owner.** Policies are silently skipped unless `FORCE ROW LEVEL SECURITY` is set, so RLS appears configured and enforces nothing.
- **A role granted `BYPASSRLS`, or an ordinary superuser connection used for migrations.** Every policy is exempted for that role, so a maintenance path reads and writes across all tenants.
- **`SECURITY DEFINER` functions.** The body executes with the definer's rights, so a function defined by an exempt role becomes a route around the policy for callers who have none.
- **Bridge migrations applied per tenant without a completion ledger.** A partial run leaves tenants on different schema versions; application code compiled against the new schema fails only for the tenants whose migration did not complete.
- **Connection pools sized per tenant under bridge.** Pool count multiplies by tenant count and the aggregate exceeds the database's connection limit, so new tenants fail to connect while existing pools sit mostly idle.
- **Rate limits keyed by user or source address in a pooled fleet.** A tenant with many users or many egress addresses stays under every per-key limit while consuming a disproportionate share of the shared fleet.
- **No `tenant_id` on tables assumed to be tenant-agnostic.** Extraction of a tenant later has no filter predicate, forcing a join-based or manual reconstruction during the migration window.
