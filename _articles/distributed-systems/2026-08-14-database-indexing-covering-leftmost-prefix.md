---
title: "Database Indexing: Covering Indexes and the Leftmost-Prefix Rule"
date: 2026-08-14
track: distributed-systems
summary: "Why a B-tree index on (a, b, c) serves a query on a and on a+b but not on b alone, how covering indexes skip the heap entirely, and the write amplification each additional index imposes."
reading_time: 7
tags: [database, indexing, b-tree, postgres, mysql]
sources:
  - title: "PostgreSQL Documentation — Index-Only Scans and Covering Indexes (§11.9)"
    url: "https://www.postgresql.org/docs/current/indexes-index-only-scans.html"
  - title: "MySQL 8.0 Reference Manual — Multiple-Column Indexes (§8.3.6)"
    url: "https://dev.mysql.com/doc/refman/8.0/en/multiple-column-indexes.html"
  - title: "Use The Index, Luke! — Concatenated Keys (Markus Winand)"
    url: "https://use-the-index-luke.com/sql/where-clause/the-equals-operator/concatenated-keys"
  - title: "Database Internals — Alex Petrov (O'Reilly, 2019)"
    url: "https://www.oreilly.com/library/view/database-internals/9781492040330/"
---

**Gist.** A predicate over a large table costs `O(n)` without an index because every row must be examined. A B-tree secondary index reduces the lookup to `O(log n)` seeks followed by a contiguous leaf walk, and a *covering* index removes the second hop to the table body as well. The mechanism imposes two costs: the sort order is meaningful only from the leading column rightwards, and every index turns one row modification into an additional B-tree modification.

## The single fact everything derives from

A secondary index is a separate B-tree keyed on one or more columns, whose leaf entries carry a pointer back to the row — a heap tuple in PostgreSQL, the clustered primary-key value in InnoDB. The tree holds keys in sorted order, so a search descends to a value in `O(log n)` and then walks leaves in key order. **The entries are sorted, and a range scan requires a contiguous run of them.** Every rule below is a consequence of that invariant.

A composite index on `(a, b, c)` is not three indexes. It is one index sorted by `a`, then by `b` within equal `a`, then by `c` within equal `(a, b)` — the ordering of a telephone directory sorted by surname, then given name.

## The leftmost-prefix rule

A contiguous run of entries exists only when the constrained columns form a prefix of the key **from the left, with no gaps**. The MySQL manual states the consequence directly: a three-column index on `(col1, col2, col3)` provides indexed search capabilities on `(col1)`, on `(col1, col2)` and on `(col1, col2, col3)`, and "MySQL cannot use the index to perform lookups if the columns do not form a leftmost prefix."

```sql
CREATE INDEX idx ON orders (customer_id, status, created_at);

-- Leftmost prefixes; the index supports the lookup:
SELECT * FROM orders WHERE customer_id = 42;
SELECT * FROM orders WHERE customer_id = 42 AND status = 'shipped';
SELECT * FROM orders WHERE customer_id = 42 AND status = 'shipped'
                       AND created_at > '2026-01-01';

-- No leading column; idx cannot drive the lookup:
SELECT * FROM orders WHERE status = 'shipped';
SELECT * FROM orders WHERE created_at > '2026-01-01';
```

Winand puts the failure case in the same directory terms: searching a two-column index by its second column alone is searching a telephone directory by given name. Rows matching a given `status` are scattered across the entire key space, so no bounded seek exists.

A second consequence of the same invariant: **a range predicate terminates the usable prefix.** Once a column is constrained with `>`, `<` or `BETWEEN`, the entries matching it span many distinct values of that column, so columns to its right are no longer sorted within the scanned run. They can filter rows already read, but cannot narrow the seek. In `WHERE customer_id = 42 AND created_at > '...' AND status = 'shipped'`, the `status` predicate is applied as a filter, not as part of the index condition. The ordering rule that follows is: **equality columns first, the range column last.**

## Reading the plan

The planner reports which predicates became seek bounds and which became filters.

```sql
EXPLAIN ANALYZE
SELECT * FROM orders WHERE customer_id = 42 AND status = 'shipped';
--  Index Scan using idx on orders  (cost=0.42..8.44 rows=1 ...)
--    Index Cond: ((customer_id = 42) AND (status = 'shipped'::text))
```

A query without the leading column yields `Seq Scan` in PostgreSQL, or an empty `key` column in MySQL's `EXPLAIN`. The distinction between the `Index Cond` line and the `Filter` line is the observable form of the prefix rule.

## Covering indexes and index-only scans

An ordinary index scan is two hops: locate the entry in the tree, then dereference the pointer to fetch the selected columns from the table body. When every column the query references is present in the index, the second hop is unnecessary; PostgreSQL calls the resulting plan an **index-only scan**. The documentation states the condition — "the query must reference only columns stored in the index" — and that B-tree indexes always support it.

In PostgreSQL, covering a column does not require widening the key: `INCLUDE` attaches non-key payload columns to the leaf entries.

```sql
-- Key remains (customer_id, status); total is carried as payload.
CREATE INDEX idx_cover ON orders (customer_id, status) INCLUDE (total);

SELECT status, total FROM orders WHERE customer_id = 42;
--  Index Only Scan using idx_cover on orders ...
```

MySQL has no `INCLUDE` clause; a column is covered there only by being part of the index key. The equivalent plan appears as `Using index` in the `Extra` column of `EXPLAIN`.

One PostgreSQL qualification is load-bearing: **row visibility is recorded in the heap, not in index entries.** An index-only scan therefore consults the **visibility map** for each page it would otherwise skip, and avoids heap access only where that page's all-visible bit is set. The bit is set by `VACUUM` and cleared by modification, so a table under heavy write churn, or one whose pages have not been vacuumed recently, produces fewer heap-free fetches than the plan name suggests.

## The write cost

An index is derived state that must be kept coherent with the table on every modification. Inserting one row into a table carrying five secondary indexes performs **one table insertion plus five B-tree insertions**. In InnoDB the table itself is a clustered B-tree, so all six are B-tree modifications; in PostgreSQL the table is a heap and only the five index writes descend a tree. Each modification may split a page, dirty a buffer, and emit write-ahead log or redo records. Updating a single indexed column requires deleting the old index entry and inserting a new one at a different sorted position, because the entry's location is determined by its key.

The trade is therefore explicit: indexes convert `O(n)` scans into `O(log n)` seeks for reads, and convert one write into `k+1` writes for `k` indexes. `INCLUDE` columns add bytes per leaf entry without adding key comparisons, which makes them a narrower way to cover a query than extending the key. Petrov's *Database Internals* describes the same tension as the defining cost of the read-optimised B-tree: maintaining sorted order in place is what makes lookups cheap and writes expensive.

### Implementation sketch (Scala)

The prefix rule is a property of lexicographic ordering on tuples, and can be exhibited without a database. The entries below are sorted by the composite key; a seek is a binary search for the lower bound of a prefix, and the matching run is contiguous only for a prefix.

```scala
import scala.math.Ordering.Implicits.* // lexicographic ordering on Vector, and `<`

type Key = Vector[String] // (customer_id, status, created_at), lexicographic

final case class Entry(key: Key, rowPtr: Long)

// Entries held in sorted key order, as B-tree leaves are.
final class SortedIndex(entries: Vector[Entry]):
  private val sorted: Vector[Entry] = entries.sortBy(_.key)

  /** Lower bound: first position whose key is >= the probe. */
  private def lowerBound(probe: Key): Int =
    var lo = 0
    var hi = sorted.length
    while lo < hi do
      val mid = lo + (hi - lo) / 2
      if sorted(mid).key < probe then lo = mid + 1 else hi = mid
    lo

  /** A seek exists only for a leading prefix; the result is one contiguous run. */
  def seekPrefix(prefix: Key): Vector[Entry] =
    sorted.view.drop(lowerBound(prefix)).takeWhile(_.key.startsWith(prefix)).toVector

  /** No prefix constrains the leading column, so every entry must be examined. */
  def scanBySecondColumn(status: String): Vector[Entry] =
    sorted.filter(_.key(1) == status)
```

`seekPrefix` costs `O(log n)` plus the size of the run; `scanBySecondColumn` costs `O(n)` on the same structure. The asymmetry is the leftmost-prefix rule: serving the second predicate cheaply requires a different index, not a better traversal of this one.

## Pitfalls

- **A range predicate placed before an equality predicate in the key order silently degrades the plan.** With `(customer_id, created_at, status)`, a query fixing `customer_id` and `status` and ranging over `created_at` shows `status` under `Filter` rather than `Index Cond`, and reads every entry in the date range.
- **An index on `(b, a)` does not serve a lookup on `a` alone.** Column order is not commutative; the directory sorted by given name does not help a search by surname.
- **An index-only scan on a write-heavy table still touches the heap.** Visibility lives in the heap, so pages whose all-visible bit is unset in the visibility map are fetched anyway, and the measured gain falls short of the plan node's name.
- **Adding a covering column to the key rather than to `INCLUDE` widens every key comparison.** The comparison cost is paid on every descent and every leaf insertion, not only by the queries that needed the column.
- **Unused indexes are invisible in read plans and fully visible in write cost.** Each one adds a B-tree modification per insert and per update of its columns, with no query referencing it.
- **`SELECT *` defeats covering.** Adding a column to the projection that is absent from the index converts an index-only scan back into an index scan plus heap fetch, with no change to the `WHERE` clause.
