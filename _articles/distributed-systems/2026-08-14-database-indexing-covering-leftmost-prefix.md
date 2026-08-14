---
title: "Database Indexing: Covering Indexes and the Leftmost-Prefix Rule"
date: 2026-08-14
track: distributed-systems
summary: "Why a B-tree index on (a, b, c) serves a query on a and a+b but not b alone, how covering indexes skip the heap entirely, and the write amplification you pay for every index you add."
reading_time: 6
tags: [database, indexing, b-tree, postgres, mysql]
sources:
  - title: "PostgreSQL Documentation — Index-Only Scans and Covering Indexes (§11.9)"
    url: "https://www.postgresql.org/docs/current/indexes-index-only-scans.html"
  - title: "MySQL 8.0 Reference Manual — Multiple-Column Indexes (§8.3.6)"
    url: "https://dev.mysql.com/doc/refman/8.0/en/multiple-column-indexes.html"
  - title: "Use The Index, Luke! — The Right Column Order in Multi-Column Indexes (Markus Winand)"
    url: "https://use-the-index-luke.com/sql/where-clause/the-equals-operator/concatenated-keys"
  - title: "Database Internals — Alex Petrov (O'Reilly, 2019)"
    url: "https://www.oreilly.com/library/view/database-internals/9781492040330/"
---

## One picture: a B-tree is a sorted phone book

A secondary index is a separate B-tree keyed on some column(s), whose leaf entries point back at the table row (a heap tuple in Postgres, the clustered primary key in InnoDB). The tree keeps its keys in sorted order, so the engine can binary-search to a value in `O(log n)` and then walk leaves in order. Everything about index behavior falls out of that one fact: **the entries are sorted, and a range scan needs a contiguous run of them.**

A composite index on `(a, b, c)` is not three indexes. It is one index sorted by `a`, then by `b` within equal `a`, then by `c` within equal `(a, b)` — exactly like a phone book sorted by surname, then first name. That ordering is the whole story behind the rules below.

## The leftmost-prefix rule

Because the tree is sorted by `a` first, you can only get a contiguous run of entries if you constrain the columns *from the left with no gaps*. MySQL's manual states it plainly: with a three-column index on `(col1, col2, col3)` you have "indexed search capabilities on `(col1)`, `(col1, col2)`, and `(col1, col2, col3)`," and "MySQL cannot use the index to perform lookups if the columns do not form a leftmost prefix."

```sql
CREATE INDEX idx ON orders (customer_id, status, created_at);

-- Uses the index (leftmost prefixes):
SELECT * FROM orders WHERE customer_id = 42;
SELECT * FROM orders WHERE customer_id = 42 AND status = 'shipped';
SELECT * FROM orders WHERE customer_id = 42 AND status = 'shipped'
                       AND created_at > '2026-01-01';

-- Cannot use idx for the lookup (no leading column):
SELECT * FROM orders WHERE status = 'shipped';
SELECT * FROM orders WHERE created_at > '2026-01-01';
```

Winand's telephone-directory analogy nails the failure case: "a two-column index does not support searching on the second column alone; that would be like searching a telephone directory by first name." The matching surnames are scattered across the whole book.

There is one more subtlety: **a range on a column stops the prefix.** Once you hit `>`, `<`, or `BETWEEN`, columns to the right can no longer be used for seeking, only for filtering. In `WHERE customer_id = 42 AND created_at > '...' AND status = 'shipped'`, the `status` predicate cannot narrow the B-tree seek because `created_at` already opened a range. Put equality columns first, the range column last.

## Confirm it with EXPLAIN

Never guess — ask the planner.

```sql
EXPLAIN ANALYZE
SELECT * FROM orders WHERE customer_id = 42 AND status = 'shipped';
--  Index Scan using idx on orders  (cost=0.42..8.44 rows=1 ...)
--    Index Cond: ((customer_id = 42) AND (status = 'shipped'::text))
```

A leading-column-less query instead shows `Seq Scan` in Postgres or drops the `key` in MySQL's `EXPLAIN`. That two-line diff is the single most useful thing to demonstrate in an interview.

## Covering indexes and index-only scans

Normally an index scan is two hops: find the entry in the tree, then follow the pointer to the heap to fetch the columns you actually selected. If *every* column the query touches already lives in the index, the engine can skip the heap hop entirely — Postgres calls this an **index-only scan**. Per the docs, it applies when "the query must reference only columns stored in the index," and B-tree indexes "always" support it.

You do not have to widen the *key* to cover extra columns. Postgres and MySQL 8 support non-key payload columns via `INCLUDE`:

```sql
-- Key stays (customer_id, status); total is carried as payload.
CREATE INDEX idx_cover ON orders (customer_id, status) INCLUDE (total);

-- Index-only scan: total is read straight from the leaf, no heap access.
SELECT status, total FROM orders WHERE customer_id = 42;
--  Index Only Scan using idx_cover on orders ...
```

In MySQL, the same win shows up as `Using index` in the `Extra` column of `EXPLAIN`.

One Postgres caveat worth knowing: visibility isn't stored in index entries, only in the heap. So an index-only scan still peeks at the **visibility map**, and only truly avoids the heap when "a large fraction of the rows are unchanging" (well-vacuumed pages with their all-visible bit set). A table churning with writes gets fewer index-only scans than the plan suggests.

## Every index taxes writes

Indexes are not free reads — they are a cache you keep coherent on every write. Insert one row into a table with five indexes and the engine performs six B-tree modifications: the table plus each index. Each may split a page, dirty a buffer, and generate WAL/redo. Update a single indexed column and that index must delete the old entry and insert the new one at a different sorted position.

So the trade is concrete: indexes turn `O(n)` scans into `O(log n)` seeks for reads, and turn one write into `k+1` writes. `INCLUDE` columns add width (bytes per leaf entry) without adding key comparisons, which is why they're the cheaper way to cover a query than stuffing everything into the key. Alex Petrov's *Database Internals* frames the same tension as the read-optimized B-tree's cost: keeping data sorted in place is what makes lookups fast and writes expensive.

The interview instinct to build: index the columns your `WHERE`/`JOIN`/`ORDER BY` actually use, ordered equality-first then range, cover the hot query if the heap hop dominates — and delete indexes nothing reads.

**Try next:** create a 10M-row table, add `(a, b, c)`, and run `EXPLAIN ANALYZE` on queries filtering by `a`, by `b` alone, and by `a` + a range on `b` + equality on `c`. Watch the plan flip between Index Scan, Seq Scan, and Index Only Scan, and confirm the range-stops-the-prefix rule in the `Index Cond` vs `Filter` lines.
