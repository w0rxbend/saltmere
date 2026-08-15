---
title: 'Pagination at Scale: Why OFFSET Degrades and Keyset Does Not'
date: 2026-08-10
track: microservices
summary: OFFSET pagination is O(offset) — the database sorts the derived table and discards every row before the requested page, so deep pages degrade while shallow ones stay fast, and concurrent inserts or deletes make rows duplicate or vanish. Keyset pagination (the seek method) stays O(limit) by riding a matching index with a row-value comparison, and its window is anchored to a value rather than a position. This article derives the cost, gives SQL for both, an opaque base64 cursor, composite keys, deletion behaviour, and the Relay and Stripe cursor shapes.
reading_time: 7
tags:
- pagination
- databases
- sql
- api-design
- postgresql
- microservices
- keyset
- cursor
- postgres
sources:
- title: We need tool support for keyset pagination (No-Offset) — Markus Winand
  url: https://use-the-index-luke.com/no-offset
- title: 'Paging Through Results: OFFSET is bad, fetch the next page — Use The Index, Luke!'
  url: https://use-the-index-luke.com/sql/partial-results/fetch-next-page
- title: GraphQL Cursor Connections Specification — Relay
  url: https://relay.dev/graphql/connections.htm
- title: Pagination — Stripe API Reference
  url: https://docs.stripe.com/api/pagination
- title: Keyset Cursors, Not Offsets, for Postgres Pagination — Sequin
  url: https://blog.sequinstream.com/keyset-cursors-not-offsets-for-postgres-pagination/
- title: Pagination — Slack Developer Docs
  url: https://docs.slack.dev/apis/web-api/pagination/
---

**Gist.** A list endpoint paged with `LIMIT ... OFFSET n` costs O(offset + limit), because the SQL semantics require the derived table to be ordered and the leading `n` rows discarded, and its window is a positional count that concurrent writes invalidate. Keyset pagination — Markus Winand's *seek method*, marketed as "No Offset" — replaces the count with a row-value predicate on the sort key of the last row returned, which a matching index turns into a range scan of cost O(limit) that is stable under inserts and deletes. The price is the loss of random access: there is no jump to page 500, and no cheap exact page count.

## Why OFFSET degrades: O(offset), not O(limit)

The apparently cheap query:

```sql
SELECT id, created_at, title
  FROM orders
 WHERE tenant_id = 42
 ORDER BY created_at DESC, id DESC
 LIMIT 20 OFFSET 300000;   -- page 15,001
```

The SQL standard defines `OFFSET` as an operation on the already-ordered derived table: **the rows are sorted first, then the leading rows are dropped**. No shortcut exists, because the identity of row 300,001 is not known until the 300,000 rows in front of it have been produced. Even with an index on `(tenant_id, created_at, id)` that supplies the order directly, the engine walks 300,020 index entries and discards 300,000 of them. The cost is therefore **O(offset + limit)**: page 1 touches 20 entries, page 15,001 touches 300,020. The asymmetry between a fast first page and a slow deep page is entirely explained by this term, and it grows without bound as the table grows.

The second defect is **instability**. An offset carries no memory of which rows were already returned; it is a blind count. If a row that sorts ahead of the current window is inserted between the fetch of page 2 and the fetch of page 3, every subsequent row shifts one position later, and the last item of page 2 is returned again as the first item of page 3 — a **duplicate**. A delete ahead of the window shifts rows the other way, and one row is **skipped entirely**. Neither event produces an error; the client sees a plausible page that omits or repeats data. Under any concurrent write traffic, offset paging is silently lossy.

## Keyset (seek method): O(limit) and stable

Keyset pagination replaces "skip n rows" with "resume strictly after the last row observed". The sort key of that row is retained and used as a filter:

```sql
SELECT id, created_at, title
  FROM orders
 WHERE tenant_id = 42
   AND (created_at, id) < (:last_created_at, :last_id)   -- row-value comparison
 ORDER BY created_at DESC, id DESC
 LIMIT 20;
```

The row-value comparison is the load-bearing construct. Under the SQL standard, `X < Y` on row values is evaluated **lexicographically, left to right**: the comparison is decided by the first component in which the two rows differ, with earlier components equal. One predicate therefore expresses "everything ordered after this exact `(created_at, id)` pair" without the expansion `created_at < :a OR (created_at = :a AND id < :b)`. PostgreSQL and current MySQL plan the row-value form as a range scan and use the composite index to **descend directly to the boundary entry**, so no row is produced and discarded. The cost is **O(limit)** independent of depth: page 1 and page 15,001 perform the same work.

Stability follows from the same change. The window is anchored to a **value**, not to an ordinal position. An insert or delete elsewhere in the ordering moves rows relative to the beginning of the result set, but the next query still starts at the first row ordered strictly after `(last_created_at, last_id)`, so no row already returned reappears and no row between the cursor and the next page is jumped over.

Two conditions are required.

1. **The sort key must be a total order.** A key with ties admits more than one valid ordering of the tied rows, and the row-value boundary can land inside the tied group, repeating or skipping members of it. Appending the primary key — `(created_at, id)` — makes the order total and the boundary unambiguous.
2. **An index must supply the requested order.** `ORDER BY created_at DESC, id DESC` needs an index in that order or in its exact reverse, which the engine can scan backwards. Otherwise the planner inserts a sort over the qualifying rows and the O(limit) property is lost.

## Opaque cursors

Exposing the raw `(created_at, id)` pair in the API invites clients to parse and depend on it, which fixes the sort order as part of the public contract. The alternative is to serialize the tuple and hand it back as an **opaque token** — a bookmark the client echoes without interpreting. Including a **version field** allows a server whose sort order has changed to reject stale tokens rather than answer them with a page computed under the wrong ordering. For untrusted clients, a message authentication code over the token prevents a forged cursor from being used to probe arbitrary key ranges, including ranges belonging to another tenant.

The request shape is `GET /orders?limit=20&cursor=…`; the response carries the page and a fresh cursor built from the last row of that page. A page shorter than `limit` indicates exhaustion.

### Implementation sketch (Scala)

```scala
import java.util.Base64
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

final case class Cursor(createdAt: Long, id: Long, version: Int = 1)

object Cursor:
  private val enc = Base64.getUrlEncoder.withoutPadding
  private val dec = Base64.getUrlDecoder

  private def sign(body: String, key: Array[Byte]): String =
    val mac = Mac.getInstance("HmacSHA256")
    mac.init(SecretKeySpec(key, "HmacSHA256"))
    enc.encodeToString(mac.doFinal(body.getBytes("UTF-8")))

  def encode(c: Cursor, key: Array[Byte]): String =
    val body = enc.encodeToString(s"${c.version}:${c.createdAt}:${c.id}".getBytes("UTF-8"))
    s"$body.${sign(body, key)}"

  def decode(token: String, key: Array[Byte]): Either[String, Cursor] =
    token.split('.') match
      case Array(body, tag) =>
        // constant-time compare avoids leaking the tag one byte at a time
        if !java.security.MessageDigest.isEqual(tag.getBytes, sign(body, key).getBytes)
        then Left("bad signature")
        else String(dec.decode(body), "UTF-8").split(':') match
          case Array(v, ts, id) if v.toInt == 1 => Right(Cursor(ts.toLong, id.toLong))
          case _                                => Left("unsupported cursor version")
      case _ => Left("malformed cursor")
```

The query then binds `c.createdAt` and `c.id` into the row-value predicate; the next cursor is built from the final row of the returned page.

## Composite sort keys and deletions

**Composite keys** widen the row value without changing the mechanism. Sorting by status, then priority, then id:

```sql
WHERE (status, priority, id) > (:last_status, :last_priority, :last_id)
ORDER BY status, priority, id
LIMIT 20;
```

All three components are encoded into the cursor. The tie-breaker `id` remains last so that the order stays total when status and priority collide.

**Deletion of the cursor row is harmless.** The predicate `(created_at, id) < (:a, :b)` is a range boundary, not a lookup of a specific row: the scan resumes at the position that value occupies in the ordering, whether or not a row with that key still exists. Offset paging has no equivalent property, because a delete changes the ordinal position of every following row.

## Cursor shapes in published APIs

**Stripe** documents `starting_after` and `ending_before` parameters carrying an object identifier — a keyset cursor over the identifier order — and returns `has_more` to signal whether further pages exist. **Relay's GraphQL Cursor Connections specification** fixes the structure: a connection exposes `edges`, each edge carrying a `node` and a `cursor`, and `pageInfo` carrying `hasNextPage`, `hasPreviousPage`, `startCursor` and `endCursor`; clients page with `first: N, after: <cursor>`. The specification requires the cursor to serialize as a **String** and treats it as opaque, which leaves the server free to encode any keyset in it. **Slack's Web API** takes the same shape under different names: a `cursor` request parameter, and a `response_metadata.next_cursor` in the reply that is empty when no further page exists.

## The trade-off

Keyset pagination gives up **random access**. Only movement relative to a held cursor is possible: forward, or backward by reversing both the comparison operator and the `ORDER BY` and then reversing the returned rows in the application. There is also no cheap exact page count, since counting requires scanning the qualifying rows. For infinite-scroll feeds, activity logs and machine-consumed list endpoints this costs nothing. Numbered pages over a small, mostly-static set remain a legitimate use of offset, where the offset term is bounded by construction.

When a dataset is spread across shards (see [data partitioning and sharding](/articles/distributed-systems/2026-08-10-data-partitioning-sharding)), the difference compounds: each shard seeks to the same `(sort_key, id)` boundary and a merge selects the global next `limit`, keeping per-shard work O(limit). A global `OFFSET 300000` has no such decomposition — the offset cannot be divided among shards without knowing the interleaving, so each shard must produce rows that the coordinator then discards.

## Pitfalls

- **Paging on a non-unique key alone.** Two rows share a `created_at`; the boundary falls between them and one is returned twice or never, because the comparison cannot distinguish them. Append the primary key.
- **Index order not matching the `ORDER BY`.** The plan gains a sort node over all qualifying rows, so latency again grows with the size of the tenant's data rather than with `limit`, even though the query text uses a cursor.
- **Mixing `ASC` and `DESC` across components of the row value.** A row-value comparison is lexicographic in one direction only; `ORDER BY a ASC, b DESC` cannot be expressed as a single `(a, b) > (:a, :b)` predicate, and writing it that way returns wrong rows without error.
- **Unsigned cursors.** A client that decodes the token can substitute arbitrary key values, including a boundary that lands in another tenant's range if the tenant predicate is derived from the cursor rather than from the authenticated session.
- **Unversioned cursors after a sort-order change.** Tokens minted under the old ordering are still accepted and interpreted against the new one, producing pages that skip or repeat arbitrary spans instead of an error.
- **Inferring end-of-data from an empty page rather than a short one.** A page equal to `limit` may still be the last; a page shorter than `limit` is the reliable signal, and waiting for an empty page adds one round trip to the end of every traversal.
