---
title: "Reading a B-tree index like the planner does: composite order, covering scans, selectivity"
date: 2026-08-13
track: distributed-systems
summary: "How a B-tree turns a lookup into O(log n), why composite-index column order is a hard rule not a preference, how to earn an index-only scan with INCLUDE, and the selectivity math that decides whether the planner even bothers."
reading_time: 6
tags: [postgres, indexing, b-tree, covering-index, query-planner, selectivity]
sources:
  - title: "PostgreSQL 18 Docs — 11.9 Index-Only Scans and Covering Indexes"
    url: "https://www.postgresql.org/docs/current/indexes-index-only-scans.html"
  - title: "PostgreSQL 18 Docs — 11.3 Multicolumn Indexes"
    url: "https://www.postgresql.org/docs/current/indexes-multicolumn.html"
  - title: "Use The Index, Luke — The Where Clause (concatenated indexes)"
    url: "https://use-the-index-luke.com/sql/where-clause/the-equals-operator/concatenated-keys"
  - title: "Fighting PostgreSQL write amplification with HOT updates (Adyen Tech)"
    url: "https://medium.com/adyen/fighting-postgresql-write-amplification-with-hot-updates-c8090f329ad6"
  - title: "Indexing Engine: Index Write Overhead (pganalyze)"
    url: "https://pganalyze.com/docs/indexing-engine/index-write-overhead"
---

An interviewer who asks "how does an index make this faster?" wants the mechanism, not "it's like a book index." A B-tree is a balanced, high-fanout tree of sorted key pages. Find one key by walking root → branch → leaf, reading one page per level. With fanout in the hundreds, a tree of a billion rows is only 4–5 levels deep, so the lookup touches ~5 pages instead of scanning the table: **O(log n)** page reads with a large logarithm base. That base is the whole trick — the height barely moves as the table grows.

## Composite indexes: column order is a rule, not a hint

A multicolumn index sorts rows by the *tuple* `(a, b, c)`: first by `a`, ties broken by `b`, then `c`. That ordering is what makes it usable, and it dictates a hard rule — an index on `(a, b, c)` can serve a query only if the query constrains a **leftmost prefix** of the columns.

```sql
CREATE INDEX idx_ev ON events (tenant_id, created_at, kind);
```

| Query predicate | Uses index? | Why |
|---|---|---|
| `tenant_id = 7` | Yes | leftmost prefix |
| `tenant_id = 7 AND created_at > now()-'1d'` | Yes, efficiently | prefix + range on next col |
| `tenant_id = 7 AND kind = 'x'` | Partly | seeks on `tenant_id`, filters `kind` in-index (gap at `created_at`) |
| `kind = 'x'` | No (seek) | `kind` isn't a prefix; planner picks a seq scan |

Two design rules fall out. Put **equality columns before range columns**: once you hit a `>` or `BETWEEN`, columns after it can't be used for seeking, only filtering. And order by **selectivity and query shape**, not alphabetically — the leading column should be one your queries almost always constrain with equality.

## Covering indexes and the index-only scan

Normally the index gets you a row pointer, then Postgres fetches the actual row from the heap — a second random read per match. If *every* column the query needs already lives in the index, Postgres can skip the heap entirely: an **index-only scan**. Add non-key payload columns with `INCLUDE` so they ride along in the leaf without affecting sort order or uniqueness (INCLUDE has been available since Postgres 11):

```sql
CREATE INDEX idx_cov ON orders (customer_id) INCLUDE (status, total_cents);

EXPLAIN (ANALYZE, BUFFERS)
SELECT status, total_cents FROM orders WHERE customer_id = 42;

--  Index Only Scan using idx_cov on orders
--    Index Cond: (customer_id = 42)
--    Heap Fetches: 0          <-- the win: zero heap access
```

`Heap Fetches: 0` is the signal you covered the query. The catch: Postgres still must confirm each row is visible to your transaction. It checks the **visibility map** — one bit per heap page, roughly four orders of magnitude smaller than the heap, so it stays cached. On a page that's been recently updated the bit is clear and Postgres falls back to a heap fetch, so `Heap Fetches` climbs on churny tables. Run `VACUUM` to set visibility bits and watch the count drop.

## Selectivity: when the planner refuses your index

An index is only worth it when the predicate is **selective** — returns a small fraction of rows. The planner estimates that fraction from `pg_statistic` (histograms, `n_distinct`) and compares the cost of ~N random index+heap reads against one big sequential scan. If a predicate matches 40% of the table, thousands of scattered random reads lose to a sequential scan that reads pages in order and prefetches. That's why an index on a low-cardinality `status` column often goes unused; a **partial index** (`WHERE status = 'pending'`) or a composite that leads with a selective column is the fix.

## When an index hurts: the write tax

Every index is a second data structure that every `INSERT`/`UPDATE`/`DELETE` must maintain. In Postgres an update writes a new row version, and normally that means inserting a new entry into **every** index on the table — write amplification that scales with index count. The escape hatch is a **HOT (Heap-Only Tuple) update**: if no *indexed* column changed and the new version fits on the same page, Postgres skips index maintenance entirely and chains the versions in the heap. Adyen's engineering team traced a throughput cliff to non-HOT updates and recovered it by dropping indexes on frequently-mutated columns and leaving `fillfactor` headroom on the page.

So the discipline: index the columns you filter and join on selectively; don't index low-cardinality or write-hot columns you rarely query; and remember every covering `INCLUDE` column widens the leaf and adds to the write tax.

**Try next:** create a table with 1M rows, add `(customer_id) INCLUDE (status)`, and run `EXPLAIN (ANALYZE, BUFFERS)` on a covered `SELECT` — note `Heap Fetches`. Now `UPDATE` 10% of rows, re-run *without* `VACUUM`, and watch `Heap Fetches` jump as visibility bits clear.
