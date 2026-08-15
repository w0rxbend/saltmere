---
title: "Database Indexing: How the B+Tree Serves a Query, and When It Does Not"
date: 2026-08-10
track: microservices
summary: "An index is a sorted, tree-shaped copy of some columns that turns an O(n) table scan into an O(log n) traversal. This article walks the B+tree that backs almost every relational index — why equality and range are both served, why leaves are linked for scans — then clustered versus secondary indexes and the bookmark lookup, composite indexes and the left-prefix rule, covering indexes and index-only scans, selectivity and why the planner declines a low-selectivity index, and the write cost that argues against indexing every column. With EXPLAIN before and after."
reading_time: 7
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

**Gist.** A predicate over an unindexed column forces the engine to inspect every row, so cost grows linearly with table size. An index maintains a second, sorted, balanced copy of one or more columns, allowing the engine to descend to the matching entries in a number of page reads logarithmic in row count. The cost is paid on the write path and in storage: every insert, and every update touching an indexed column, must modify each affected index tree in addition to the table.

## The B+tree: equality and range from one structure

Almost every relational index is a B+tree; the term "B-tree" is used loosely, but the leaf-linked variant is what ships in the major engines. Three properties are load-bearing.

**The structure is sorted and balanced.** Branch nodes hold separator keys pointing down to child nodes; leaf nodes hold the index entries in key order. A lookup starts at the root and follows references downward. Because each node holds many entries, the tree stays shallow: Use The Index, Luke! states that **"the tree depth grows very slowly compared to the number of leaf nodes"**. That relationship is the `log(n)` in an index lookup — even a very large table is only a few page reads deep, and doubling the row count adds far less than a doubling of the work.

**Leaf nodes form a doubly linked list.** The leaves are chained in logical key order regardless of their physical placement on disk. This is what makes *range* access efficient: the engine descends once to locate the first qualifying entry for `WHERE created_at >= '2026-01-01'`, then **walks the leaf chain forward, reading entries that are already in sorted order**. Equality access is "descend to the leaf"; range access is "descend, then follow the chain". The same ordering also allows `ORDER BY` and `GROUP BY` on the index columns to be satisfied without a separate sort step. A hash index, by contrast, answers equality in O(1) but cannot answer a range predicate at all, because hashing destroys order.

The write path is where this family differs from the other dominant storage-engine design; see [LSM-Trees vs B-Trees](/articles/distributed-systems/2026-08-10-lsm-trees-vs-b-trees).

## Clustered and secondary indexes, and the bookmark lookup

The remaining question is where the *rows* live, and that depends on the index type.

A **clustered index** stores the full row in its leaf nodes: the table is the index, ordered by that key. In InnoDB the primary key is the clustered index, and the MySQL manual states that "accessing a row through the clustered index is fast because the index search leads directly to the page that contains the row data" ([MySQL 8.4 manual](https://dev.mysql.com/doc/refman/8.4/en/innodb-index-types.html)). When no primary key is declared, InnoDB uses the first non-null unique index, or synthesizes a hidden row identifier.

A **secondary index** — any non-clustered index — does not store the row. In InnoDB, "each record in a secondary index contains the primary key columns for the row, as well as the columns specified for the secondary index." A read through a secondary index therefore has **two descents**: descend the secondary tree to the matching entry, read the primary key stored there, then descend the clustered index by that primary key to reach the row. The second descent is the **bookmark lookup**. PostgreSQL performs the analogous step by storing a heap tuple pointer in the index entry and issuing a heap fetch.

Two consequences follow directly. A secondary-index read costs strictly more than a primary-key read on the same row. And because every secondary index entry embeds the primary key, a wide primary key inflates every secondary index on the table — hence the manual's advice that "it is advantageous to have a short primary key."

## Composite indexes and the left-prefix rule

A composite (concatenated) index on `(a, b, c)` is sorted by `a`, then by `b` within equal values of `a`, then by `c` within equal `(a, b)`. The **left-prefix rule** follows from that ordering alone: the index can serve searches on a leading prefix of its columns. Use The Index, Luke! puts it as "an index with three columns can be used when searching for the first column, when searching with the first two columns together, and when searching using all columns."

The telephone-directory analogy is exact. A directory ordered by `(surname, first_name)` locates every "Winand", and "Winand, Markus" — but is useless for locating every "Markus", because entries with that first name are scattered across every page. A two-column index does not support searching on the second column alone.

For an index on `(status, created_at)`:

- `WHERE status = 'OPEN'` — served, leading column.
- `WHERE status = 'OPEN' AND created_at > now() - interval '1 day'` — served, both columns.
- `WHERE created_at > ...` alone — **not served**; the entries are scattered, and a full scan is the likely plan.

Column order therefore determines which queries the index can answer. The frequently repeated instruction to put the most selective column first is a [myth](https://use-the-index-luke.com/sql/myth-directory/most-selective-first): the useful order is the one that supports the largest set of queries, placing equality predicates ahead of range predicates so that the range walk is confined to a contiguous leaf region. The same ordering property is what allows keyset [pagination](/articles/microservices/2026-08-10-pagination-offset-vs-keyset) to ride an index.

## EXPLAIN, before and after

Consider a 5-million-row `orders(id PK, customer_id, status, created_at, total)` with no secondary index. The plan fragments below are illustrative and abridged — the shape of each plan is the point; the absolute cost estimates depend on hardware, cache state and data distribution.

```sql
EXPLAIN ANALYZE
SELECT id, total FROM orders
WHERE customer_id = 42 AND status = 'OPEN';
```
```
Seq Scan on orders  (cost=0.00..96000 rows=6 width=12)
  Filter: (customer_id = 42 AND status = 'OPEN')
  Rows Removed by Filter: 4999994
```

The sequential scan reads all five million rows and discards 4,999,994. Adding a composite index with the equality columns leading:

```sql
CREATE INDEX idx_orders_cust_status ON orders (customer_id, status);
```
```
Index Scan using idx_orders_cust_status on orders  (cost=0.43..25.9 rows=6)
  Index Cond: (customer_id = 42 AND status = 'OPEN')
```

The scan cost is proportional to table size while the index scan is not, so **the gap widens as the table grows**. The plan is still an `Index Scan` rather than an index-only scan, because `total` is not in the index: six bookmark lookups remain.

## Covering indexes and index-only scans

When the index holds every column the query references, the engine can skip the table access entirely — an **index-only scan**. The PostgreSQL documentation names two requirements: a B-tree index, which "always" supports index-only scans, and that "the query must reference only columns stored in the index." A payload column can be carried without joining the sort key by using `INCLUDE`:

```sql
CREATE INDEX idx_orders_cover
  ON orders (customer_id, status) INCLUDE (total);
```
```
Index Only Scan using idx_orders_cover on orders  (cost=0.43..8.9 rows=6)
  Index Cond: (customer_id = 42 AND status = 'OPEN')
  Heap Fetches: 0
```

`Heap Fetches: 0` records that no bookmark lookup occurred. The `INCLUDE` columns are stored in the leaf without participating in the ordering. In MySQL/InnoDB the equivalent is to add the column to the index key itself, and EXPLAIN reports `Using index`. One PostgreSQL qualification: an index-only scan must still establish that each row is visible to the transaction's MVCC snapshot, which it does via the visibility map; on a heavily updated table the required pages may not be marked all-visible, and the plan falls back to heap fetches until `VACUUM` runs.

### Implementation sketch (Scala)

The left-prefix rule is a property of lexicographic ordering, not of any engine internal. A sorted map keyed by a tuple exhibits the same behaviour: a prefix predicate maps to a contiguous range, a non-prefix predicate does not.

```scala
// Index entries ordered by (customerId, status); value is the "bookmark".
type Key = (Long, String)
val index: scala.collection.immutable.TreeMap[Key, Long] = ???

// Leading-column predicate: one contiguous leaf range.
def byCustomer(customerId: Long): Iterable[Long] =
  index.rangeFrom((customerId, "")).takeWhile(_._1._1 == customerId).values

// Full-key equality: descend to a single point in the same ordering.
def byCustomerAndStatus(customerId: Long, status: String): Option[Long] =
  index.get((customerId, status))

// Non-leading column alone: no contiguous range exists, so every entry
// must be inspected — the structural reason the planner chooses a scan.
def byStatusOnly(status: String): Iterable[Long] =
  index.collect { case ((_, s), row) if s == status => row }
```

`rangeFrom` is O(log n) to position plus O(k) to walk k matches, mirroring descend-then-follow-the-chain. `collect` is O(n) regardless of how few rows match.

## Selectivity and planner choice

An index is worth traversing only if it eliminates most of the table. **Selectivity** — the fraction of rows a predicate admits — and column cardinality drive the planner's decision. An index on a `status` column with three values, each covering roughly a third of the rows, is low-selectivity: retrieving a third of the table through the index means that many bookmark lookups in index order rather than physical order, which can cost more than reading the table sequentially. A planner that declines the index in this case is behaving correctly, not failing. A plain index on a balanced boolean such as `is_active` is the canonical example: only when one value is rare — in which case a partial index on that value is the better instrument — does the index earn its traversal. Indexes pay off on high-selectivity predicates: user identifiers, email addresses, timestamps.

## Write amplification

Every index is an additional tree that must be kept consistent with the table. Each `INSERT` writes an entry into every index on the table; each `UPDATE` to an indexed column rewrites the corresponding leaf entry, and in InnoDB a change to the primary key touches every secondary index, since each stores the primary key. **Ten secondary indexes turn one row insert into ten index-tree modifications on top of the write into the clustered index or heap.** Indexes also occupy storage and buffer-pool memory that would otherwise hold table pages. The resulting discipline: index for the queries the workload issues, prefer one composite index that serves several queries over several single-column indexes, drop indexes that no observed plan selects, and account for the per-write cost on write-heavy tables.

## Pitfalls

- **A query filters on the second column of a composite index and the plan shows a sequential scan.** The left-prefix rule is not satisfied; matching entries are scattered across every leaf, so no contiguous range exists to walk.
- **An index on a low-cardinality column is never chosen.** Retrieving a large fraction of the rows through bookmark lookups in index order costs more than a sequential read of the table, and the planner's estimate reflects that.
- **An `Index Only Scan` reports non-zero `Heap Fetches` in PostgreSQL.** The visibility map does not mark the relevant pages as all-visible, so visibility must be checked in the heap; recent write activity without a `VACUUM` produces this.
- **Secondary indexes grow unexpectedly large in InnoDB.** Each secondary index entry embeds the primary key columns, so a wide primary key is replicated into every secondary index.
- **Insert throughput drops after adding indexes to a write-heavy table.** Each insert must modify every index tree, so the per-row write cost scales with the number of indexes.
- **A query that appears to match the index still reads the table.** The index does not contain a column in the select list, so each matching entry requires a bookmark lookup into the clustered index or heap.
