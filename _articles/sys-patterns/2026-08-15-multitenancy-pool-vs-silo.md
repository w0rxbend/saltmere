---
title: "Multi-Tenancy Patterns: Pool, Silo, and the Bridge Between"
date: 2026-08-15
track: sys-patterns
summary: "AWS's SaaS Lens names the three tenancy models every SaaS interview circles: silo (dedicated stack per tenant), pool (shared everything plus a tenant_id column), and bridge (shared compute, per-tenant database or schema). The real content is the trade-offs — noisy neighbors and blast radius in pool, cost and operational sprawl in silo — and the enforcement mechanics: Postgres row-level security with a current_setting-driven policy, tenant-aware token buckets, and tiering that pools free tenants while siloing the enterprise contracts that demand it. Shopify's pods architecture shows the same idea applied at platform scale."
reading_time: 6
tags: [multi-tenancy, saas, silo-pool-bridge, row-level-security, postgres, noisy-neighbor, cell-based]
sources:
  - title: "AWS Well-Architected SaaS Lens — Silo, Pool, and Bridge Models"
    url: "https://docs.aws.amazon.com/wellarchitected/latest/saas-lens/silo-pool-and-bridge-models.html"
  - title: "AWS Whitepaper — SaaS Tenant Isolation Strategies: Pool Isolation"
    url: "https://docs.aws.amazon.com/whitepapers/latest/saas-tenant-isolation-strategies/pool-isolation.html"
  - title: "PostgreSQL Documentation — 5.9. Row Security Policies"
    url: "https://www.postgresql.org/docs/current/ddl-rowsecurity.html"
  - title: "Shopify Engineering — A Pods Architecture to Allow Shopify to Scale"
    url: "https://shopify.engineering/a-pods-architecture-to-allow-shopify-to-scale"
---

Every SaaS architecture conversation eventually arrives at the same fork: does each customer get their own stack, or does everyone share one with a `tenant_id` column? AWS's Well-Architected **SaaS Lens** gave the fork its standard names — **silo**, **pool**, and **bridge** — and the vocabulary is worth adopting because it turns a vague "it depends" into a checklist of specific costs: isolation, noisy neighbors, blast radius, per-tenant cost attribution, and how much sleep you lose during migrations.

## The three models

**Silo** gives each tenant dedicated infrastructure — at the extreme, a full stack (VPC, compute, database) per tenant. Isolation is structural: a runaway query from tenant A physically cannot slow tenant B, compliance conversations get easy ("your data lives in your database"), and per-tenant cost is just the AWS bill for their silo. The price is everything else: idle capacity multiplied by tenant count, deployments that scale O(tenants), config drift, and onboarding that means provisioning infrastructure rather than inserting a row.

**Pool** shares everything: one fleet, one database, every table carrying `tenant_id`. It's the economic default — utilization is high, deploys are singular, a new tenant costs one insert. The price is that isolation becomes a *software* problem. Every query must be tenant-scoped, one tenant's traffic spike is everyone's latency (**noisy neighbor**), and a bad deploy or a dropped index takes down all tenants at once — maximal **blast radius**.

**Bridge** is the pragmatic hybrid the SaaS Lens calls out: shared application tier, per-tenant *data* — either a database per tenant or a schema per tenant in one cluster. You keep one deployable fleet while data isolation, per-tenant backup/restore ("restore only Acme to yesterday 14:00" is trivial in bridge, forensic surgery in pool), and per-tenant encryption keys come structurally. The costs are connection-pool fanout (500 tenants × per-schema pools), migrations that run 500 times, and schema drift between tenants.

| | Silo | Bridge | Pool |
|---|---|---|---|
| Isolation | Structural | Structural for data, shared compute | Software-enforced |
| Noisy neighbor | None | Compute-level only | Full exposure |
| Cost/tenant | Highest, but attributable | Medium | Lowest, hard to attribute |
| Onboarding | Provision infra (minutes–days) | Create db/schema | Insert a row |
| Ops burden | O(tenants) deploys/config | One fleet, O(tenants) migrations | One of everything |
| Per-tenant restore | Trivial | Trivial | Painful |

## Enforcing pool isolation: RLS, not WHERE-clause discipline

The pool model's classic failure is the one missing `WHERE tenant_id = ?` — a cross-tenant data leak, which is an incident report and sometimes a lost enterprise deal. AWS's tenant-isolation whitepaper is blunt that pooled storage requires an enforcement layer *beneath* application code. In Postgres that's **row-level security**: policies attached to the table that the query planner applies to every statement, regardless of what the application forgot.

```sql
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;  -- apply to table owner too

CREATE POLICY tenant_isolation ON documents
  USING (tenant_id = current_setting('app.current_tenant')::uuid);

-- per-request, after authenticating the tenant:
SET LOCAL app.current_tenant = 'b7f2…';   -- inside the transaction
SELECT * FROM documents WHERE title ILIKE '%q3%';  -- planner adds tenant filter
```

`SET LOCAL` scopes the tenant to the transaction, which matters when connections are pooled and reused across tenants. Two footguns from the Postgres docs: superusers and roles with `BYPASSRLS` skip policies entirely, and table owners skip them unless you `FORCE` — so the app must connect as a plain role that owns nothing. RLS costs a predicate per query (usually index-friendly); what it buys is turning "every engineer remembers the WHERE clause forever" into "the database refuses."

Compute-side, pooled tenants need **tenant-aware throttling**: admission control keyed by tenant, not just by user or IP — a token bucket per tenant with tier-based rates, plus fair queueing in workers so one tenant's 10M-row export doesn't monopolize the pool. The mechanics are the standard [rate-limiting toolkit](/articles/microservices/2026-07-31-rate-limiting-load-shedding-token-bucket) applied at tenant granularity; [shuffle sharding](/articles/sys-patterns/2026-07-31-shuffle-sharding) is the complementary trick that keeps one poisonous tenant from taking out every worker it touches.

## Tiering: charge for the silo

The models compose per *tier*, and this is usually the strongest interview answer: free and self-serve tenants live pooled (their economics demand it), while enterprise contracts — the ones with compliance addenda, data-residency clauses, and seven-figure renewals — get bridge or full silo. The premium tier literally sells reduced blast radius. The same composition applies per *component*: pool the stateless API for everyone, silo only the storage, which is where most isolation requirements actually bite.

## Pods, cells, and migrating between models

Scaled out, the silo idea stops being per-tenant and becomes per-*group*: Shopify's **pods architecture** partitions the platform into pods, each a fully isolated slice — its own MySQL, Redis, and workers — owning a fixed subset of shops. A pod failure strands only its shops; capacity scales by adding pods; flash-sale whales get isolated by placement. This is [cell-based architecture](/articles/sys-patterns/2026-07-30-cell-based-architecture) with tenancy as the partition key — silo-of-pools: each cell is internally a pool, but the fleet is a set of silos with bounded blast radius.

That framing also makes **migration between models** concrete, because you will migrate: the pooled tenant that grows into a noisy neighbor needs extraction to its own cell or silo. The playbook is dual-write or CDC-replicate the tenant's rows (filtered by `tenant_id` — another reason the column is non-negotiable even in bridge), verify counts and checksums, flip the tenant's routing entry in the tenant catalog, then delete the pooled copy. The **tenant catalog** — the mapping from tenant to model, cell, database, and tier — is the one component every multi-tenant design needs on the whiteboard, because it's what makes the models an implementation detail behind a stable routing layer rather than a one-way architectural door.

**Try next:** build the RLS demo above in Postgres with two tenants and 1M rows, connect as a non-owner role, and try to leak data — via a missing `SET LOCAL`, a view, and a function marked `SECURITY DEFINER` — then run `EXPLAIN ANALYZE` with and without RLS to measure what the policy predicate actually costs on an indexed query.
