---
title: "Reading a B-tree index like the planner does: composite order, covering scans, selectivity"
date: 2026-08-13
track: distributed-systems
summary: "How a B-tree reduces a lookup to O(log n) page reads, why composite-index column order is a hard rule rather than a preference, what INCLUDE buys for an index-only scan, and the selectivity estimate that decides whether the planner uses the index at all."
reading_time: 7
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

**Gist.** Locating a row by predicate without an index requires reading every heap page. A B-tree — a balanced, high-fanout tree of sorted key pages — reduces the same lookup to one page read per tree level, and because fanout is in the hundreds a tree over a very large table is only a handful of levels deep: **O(log n) page reads with a large logarithm base**, so height barely moves as the table grows. The cost is that the index is a second data structure every `INSERT`, `UPDATE` and `DELETE` must maintain, and that maintenance is charged on the write path whether or not any query uses the index.

## The mechanism: descent by sorted key

A B-tree lookup walks root → branch → leaf. Each internal page holds separator keys and child pointers in sorted order; a binary search within the page selects one child, and the descent repeats. The invariant that makes this correct is that **every key in a subtree lies within the separator range that points to it**, and that all leaves sit at the same depth — the tree is balanced by construction, so no key is more expensive to find than any other. The leaf holds the key together with a pointer to the heap row.

Two consequences follow from the leaves being sorted rather than hashed. A range predicate (`>`, `BETWEEN`) is served by descending once to the first qualifying key and then walking the leaf level sequentially. And an `ORDER BY` matching the index key order can be answered by the scan itself, with no separate sort step.

## Composite indexes: column order is a rule

A multicolumn index sorts entries by the *tuple* `(a, b, c)`: by `a` first, ties broken by `b`, then by `c`. That ordering is the whole basis of usability, and it imposes a hard rule — an index on `(a, b, c)` can be used for seeking only where the query constrains a **leftmost prefix** of the columns.

```sql
CREATE INDEX idx_ev ON events (tenant_id, created_at, kind);
```

| Query predicate | Uses index? | Why |
|---|---|---|
| `tenant_id = 7` | Yes | leftmost prefix |
| `tenant_id = 7 AND created_at > now()-'1d'` | Yes, efficiently | prefix + range on next col |
| `tenant_id = 7 AND kind = 'x'` | Partly | seeks on `tenant_id`, filters `kind` in-index (gap at `created_at`) |
| `kind = 'x'` | No (seek) | `kind` isn't a prefix, so no descent narrows the range |

Two design rules follow. **Equality columns belong before range columns**: after the first `>` or `BETWEEN`, the entries matching that range are contiguous in the leaf but are no longer sorted by the columns that follow, so those columns can be used to filter rows already read, not to narrow the seek. And column order should follow selectivity and the query shape, not the alphabet — the leading column should be one the queries constrain with equality almost every time.

### Implementation sketch (Scala)

The prefix rule is legible as ordinary tuple comparison. Entries are sorted by the composite key; a seek is a binary search for the lower bound of the constrained prefix, and everything after the prefix gap is a residual filter applied to rows already fetched.

```scala
type Key = Vector[String]                       // one component per indexed column

final case class Entry(key: Key, rowPtr: Long)

// Leaf entries kept in composite-key order; lexicographic on the component vector.
val keyOrd: Ordering[Key] = Ordering.Implicits.seqOrdering[Vector, String]

final class CompositeIndex(entries: Vector[Entry]):
  private val sorted = entries.sortBy(_.key)(using keyOrd)

  /** Descend to the first entry >= prefix, then walk while the prefix still matches. */
  def seek(prefix: Key): Iterator[Entry] =
    val lo = lowerBound(prefix)
    sorted.iterator.drop(lo).takeWhile(_.key.startsWith(prefix))

  private def lowerBound(prefix: Key): Int =
    var lo = 0
    var hi = sorted.length
    while lo < hi do
      val mid = (lo + hi) >>> 1
      // Compare only the constrained components: a shorter prefix bounds a whole subrange.
      if keyOrd.lt(sorted(mid).key.take(prefix.length), prefix) then lo = mid + 1
      else hi = mid
    lo

// A predicate on a non-prefix column cannot narrow the seek; it only filters the scan.
def byTenantAndKind(ix: CompositeIndex, tenant: String, kind: String): Iterator[Entry] =
  ix.seek(Vector(tenant)).filter(_.key(2) == kind)
```

## Covering indexes and the index-only scan

By default the index yields a row pointer and PostgreSQL then fetches the row from the heap — **one additional random read per match**. If every column the query needs is already present in the index, the heap access can be skipped: an **index-only scan**. Non-key payload columns are added with `INCLUDE`, so they are stored in the leaf without participating in sort order or in uniqueness (`INCLUDE` has been available since PostgreSQL 11):

```sql
CREATE INDEX idx_cov ON orders (customer_id) INCLUDE (status, total_cents);

EXPLAIN (ANALYZE, BUFFERS)
SELECT status, total_cents FROM orders WHERE customer_id = 42;

--  Index Only Scan using idx_cov on orders
--    Index Cond: (customer_id = 42)
--    Heap Fetches: 0          <-- zero heap access
```

`Heap Fetches: 0` is the signal that the query is covered. The qualification is visibility: an index entry does not record which transactions may see the row, so PostgreSQL consults the **visibility map**, which stores a small fixed number of bits per heap page — among them the all-visible bit — and is therefore orders of magnitude smaller than the heap, small enough to stay cached. Where a page has been recently updated the bit is clear and the scan falls back to a heap fetch for rows on that page, so `Heap Fetches` climbs on churning tables. `VACUUM` sets visibility bits and the count falls again.

## Selectivity: when the planner declines the index

An index pays only where the predicate is **selective** — matching a small fraction of rows. The planner estimates that fraction from `pg_statistic` (histograms, `n_distinct`) and compares the cost of roughly N random index-plus-heap reads against a single sequential scan. Where a predicate matches a large fraction of the table, scattered random reads lose to a sequential scan that reads pages in physical order and prefetches. This is the usual reason an index on a low-cardinality `status` column is never chosen; a **partial index** (`WHERE status = 'pending'`) or a composite leading with a selective column changes the estimate.

## The write tax

Every index is a second structure maintained on every write. In PostgreSQL an `UPDATE` writes a new row version, which ordinarily means inserting a new entry into **every** index on the table — write amplification proportional to index count. The exception is a **HOT (Heap-Only Tuple) update**: where no *indexed* column changed and the new version fits on the same heap page, index maintenance is skipped and the versions are chained within the heap. Adyen's engineering team reports write amplification from non-HOT updates and describes two levers against it: not indexing frequently mutated columns, and lowering `fillfactor` so each heap page keeps room for a new version.

The resulting discipline: index the columns used for selective filters and joins; avoid indexing low-cardinality or write-hot columns that are rarely queried; and account for each `INCLUDE` column widening the leaf and adding to the write tax.

## Pitfalls

- **A predicate on a non-leading column cannot seek.** `kind = 'x'` against `(tenant_id, created_at, kind)` has no usable prefix, so no descent narrows the range and the planner falls back to reading the whole table or the whole index.
- **A range column placed before an equality column truncates the seek.** Entries after the range boundary are contiguous but unsorted by later columns, so those columns filter rows already read instead of narrowing the search.
- **`Heap Fetches` rising after an update burst is a visibility-map effect, not index corruption.** Updates clear the all-visible bit on the affected heap pages, and the index-only scan falls back to heap access until `VACUUM` resets the bits.
- **`INCLUDE` columns do not enforce uniqueness and do not affect ordering.** A unique index on `(a) INCLUDE (b)` constrains `a` alone; adding `b` to `INCLUDE` does not make `(a, b)` the key.
- **An index on a frequently mutated column suppresses HOT updates.** Once the column is indexed, every update to it must insert into every index on the table, so throughput degrades in proportion to index count.
- **A low-selectivity predicate leaves the index unused but still charged.** The write cost is paid on every insert and update regardless of whether any plan chooses the index.
