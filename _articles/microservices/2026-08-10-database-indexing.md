---
title: "Database Indexing: How the B+Tree Makes Your Query Fast (and Why It Sometimes Doesn't)"
date: 2026-08-10
track: microservices
summary: "An index is a sorted, tree-shaped copy of some columns that turns an O(n) table scan into an O(log n) traversal. This walks the B+tree that powers almost every relational index — why equality and range are both fast, why leaves are linked for scans — then the parts interviewers actually probe: clustered vs secondary indexes and the bookmark lookup, composite indexes and the left-prefix rule, covering indexes and index-only scans, selectivity and why the planner ignores a bad index, and the write cost that means you should not index everything. With EXPLAIN before/after."
reading_time: 6
tags:
  - databases
  - indexing
  - sql
  - postgresql
  - mysql
  - performance
  - microservices
sources:
  - title: "The Tree — SQL index anatomy (B-tree), Use The Index, Luke!"
    url: "https://use-the-index-luke.com/sql/anatomy/the-tree"
  - title: "The Right Column Order in Multi-Column (Concatenated) Indexes — Use The Index, Luke!"
    url: "https://use-the-index-luke.com/sql/where-clause/the-equals-operator/concatenated-keys"
  - title: "MySQL 8.4 Reference Manual: 17.6.2.1 Clustered and Secondary Indexes"
    url: "https://dev.mysql.com/doc/refman/8.4/en/innodb-index-types.html"
  - title: "PostgreSQL Documentation: 11.9 Index-Only Scans and Covering Indexes"
    url: "https://www.postgresql.org/docs/current/indexes-index-only-scans.html"
  - title: "Myth: Put the Most Selective Column First — Use The Index, Luke!"
    url: "https://use-the-index-luke.com/sql/myth-directory/most-selective-first"
---

Your query is slow because the database is reading every row to find the few you asked for. An index fixes that by keeping a second, sorted, tree-shaped copy of one or more columns so the engine can *navigate* to your rows instead of scanning past all the others. This is the single highest-leverage thing most engineers can learn about databases, and it is a system-design interview staple. Let us build up from the data structure.

## The B+tree: why equality *and* range are both fast

Almost every relational index is a B+tree (people say "B-tree", but the leaf-linked variant is what ships). It has three properties that matter.

**It is sorted and balanced.** Branch nodes hold separator keys pointing down to child nodes; leaf nodes hold the actual index entries in key order. To find a value you start at the root and follow references downward, and because each node holds hundreds of entries, the tree stays shallow. As Use The Index, Luke! puts it, "the tree depth grows very slowly compared to the number of leaf nodes" — with a modest branching factor a tree of depth 10 already addresses over a million entries. That is the `log(n)` in an index lookup: a table of a billion rows is only a handful of page reads deep.

**Leaf nodes are a doubly linked list.** The leaves are chained in logical order regardless of where they sit on disk. That is what makes *range* scans fast: find `WHERE created_at >= '2026-01-01'` once via tree descent, then walk the leaf chain forward reading already-sorted entries. Equality is "descend to the leaf"; range is "descend, then follow the chain." Same structure serves both, plus `ORDER BY` and `GROUP BY` for free because the data is already ordered. (A hash index, by contrast, does O(1) equality but cannot do ranges at all.)

This is also the fault line with LSM-trees, the other storage engine family — see [LSM-Trees vs B-Trees](/articles/distributed-systems/2026-08-10-lsm-trees-vs-b-trees) for how the write path differs.

## Clustered vs secondary indexes and the bookmark lookup

Where do the *rows* live? That depends on the index type.

A **clustered index** stores the full row in its leaf nodes — the table *is* the index, sorted by that key. In InnoDB the primary key is the clustered index: "Accessing a row through the clustered index is fast because the index search leads directly to the page that contains the row data" ([MySQL docs](https://dev.mysql.com/doc/refman/8.4/en/innodb-index-types.html)). If you declare no primary key, InnoDB picks the first non-null unique index, or synthesizes a hidden row ID.

A **secondary index** (any non-clustered index) does *not* store the row. In InnoDB, "each record in a secondary index contains the primary key columns for the row, as well as the columns specified for the secondary index." So a lookup by secondary index is two steps: descend the secondary tree to find the matching entry, read the primary key stored there, then descend the *clustered* index by that PK to fetch the row. That second descent is the **bookmark lookup** (Postgres does the analogous thing by storing a heap tuple pointer and doing a heap fetch). It is why a secondary-index read costs more than a primary-key read, and why a long primary key bloats every secondary index — "it is advantageous to have a short primary key."

## Composite indexes and the left-prefix rule

The most misunderstood index — and the most common interview question. A composite (concatenated) index on `(a, b, c)` is sorted by `a`, then by `b` within equal `a`, then by `c`. The consequence is the **left-prefix rule**: the index can serve searches on the leading columns only. Per Use The Index, Luke!, "an index with three columns can be used when searching for the first column, when searching with the first two columns together, and when searching using all columns."

The telephone-directory analogy nails it: a directory sorted by `(surname, first_name)` lets you find everyone named "Winand", or "Winand, Markus" — but is useless for finding every "Markus", because first names are scattered across every page. "A two-column index does not support searching on the second column alone."

So an index on `(status, created_at)`:

- `WHERE status = 'OPEN'` — uses it (leading column)
- `WHERE status = 'OPEN' AND created_at > now() - interval '1 day'` — uses both
- `WHERE created_at > ...` alone — **cannot** use it; likely a full scan

Column order is therefore the whole game, and the popular "put the most selective column first" advice is a [myth](https://use-the-index-luke.com/sql/myth-directory/most-selective-first) — the right order is the one that supports the most queries and puts equality predicates before range predicates. This ordering is also exactly what makes keyset [pagination](/articles/microservices/2026-08-10-pagination-offset-vs-keyset) ride the index.

## EXPLAIN, before and after

Take a 5-million-row `orders(id PK, customer_id, status, created_at, total)` with no secondary index:

```sql
EXPLAIN ANALYZE
SELECT id, total FROM orders
WHERE customer_id = 42 AND status = 'OPEN';
```
```
Seq Scan on orders  (cost=0.00..96000 rows=6 width=12)
  Filter: (customer_id = 42 AND status = 'OPEN')
  Rows Removed by Filter: 4999994
  Execution Time: 812.4 ms
```

A sequential scan reads all 5M rows and throws away 4,999,994. Add a composite index with equality columns leading:

```sql
CREATE INDEX idx_orders_cust_status ON orders (customer_id, status);
```
```
Index Scan using idx_orders_cust_status on orders  (cost=0.43..25.9 rows=6)
  Index Cond: (customer_id = 42 AND status = 'OPEN')
  Execution Time: 0.19 ms
```

From 812 ms to 0.19 ms. But note the plan still visits the heap/clustered index for `total` — that is the bookmark lookup, six of them here.

## Covering indexes and index-only scans

If the index already holds *every* column the query touches, the engine can skip the table entirely — an **index-only scan**. Postgres requires two things: a B-tree index (which "always" supports it), and that "the query must reference only columns stored in the index." Include the payload column with `INCLUDE`:

```sql
CREATE INDEX idx_orders_cover
  ON orders (customer_id, status) INCLUDE (total);
```
```
Index Only Scan using idx_orders_cover on orders  (cost=0.43..8.9 rows=6)
  Index Cond: (customer_id = 42 AND status = 'OPEN')
  Heap Fetches: 0
```

`Heap Fetches: 0` — no bookmark lookups at all. The `INCLUDE` columns ride along in the leaf without being part of the sort key. (In MySQL/InnoDB you cover by adding the column to the index key, and EXPLAIN shows `Using index`.) One Postgres caveat: index-only scans still must confirm each row is visible to your MVCC snapshot via the visibility map, so a heavily-updated table can silently fall back to heap fetches until `VACUUM` runs.

## Selectivity, and why the planner ignores your index

An index is only worth using if it eliminates most of the table. **Selectivity** (distinct values ÷ rows; high = selective) and **cardinality** drive the planner's choice. An index on `status` with three values each covering a third of the table is low-selectivity: reading 33% of rows through an index means random-ordered bookmark lookups, which is *slower* than a sequential scan. So the planner correctly ignores it and scans. This is not a bug — a boolean `is_active` column is a classic index-that-never-gets-used. Indexes earn their keep on high-selectivity predicates: user IDs, emails, timestamps.

## When *not* to index: write amplification

Every index is a second tree that must be kept in sync. Each `INSERT` writes every index; each `UPDATE` to an indexed column rewrites its leaf entry (and secondary indexes when the PK moves). Ten indexes turn one row insert into eleven tree modifications. Indexes also consume storage and RAM. So the discipline is: index for the queries you actually run, prefer composite indexes that serve several queries over many single-column ones, drop indexes that EXPLAIN never chooses, and remember that a write-heavy table pays for every index on every write.

**Try next:** Run `EXPLAIN (ANALYZE, BUFFERS)` on your slowest endpoint's query, find the `Seq Scan` with the biggest `Rows Removed by Filter`, and add one composite index with equality columns first — then confirm you can turn it into an `Index Only Scan` with `INCLUDE`.
