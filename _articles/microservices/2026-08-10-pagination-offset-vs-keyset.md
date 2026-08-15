---
title: 'Pagination at Scale: Why OFFSET Falls Over and Keyset Wins'
date: 2026-08-10
track: microservices
summary: OFFSET pagination is O(offset) — the database scans and throws away every row before the page you want, so page 10,000 crawls and concurrent inserts make rows duplicate or vanish. Keyset (seek-method / cursor) pagination stays O(limit) by riding the index with a row-value WHERE clause, and it is stable under writes. Here is the Big-O framing, concrete SQL for both, a base64 opaque-cursor encode/decode, composite keys, deletions, and how Relay- and Stripe-style API cursors are built.
reading_time: 6
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

You built a list endpoint. `?page=2&size=20` works fine in the demo. Six months later a customer with 400,000 orders opens page 15,000 and the request times out — while the customer on page 1 is fast. Nothing changed except the offset. That asymmetry is the whole story, and it is a classic system-design interview probe: *how does your pagination behave at page N, and what happens when someone inserts a row mid-scan?*

## Why OFFSET degrades: O(offset), not O(limit)

The obvious query looks cheap:

```sql
SELECT id, created_at, title
  FROM orders
 WHERE tenant_id = 42
 ORDER BY created_at DESC, id DESC
 LIMIT 20 OFFSET 300000;   -- page 15,001
```

The SQL standard is explicit about what `OFFSET` means: the derived table is *first sorted, then the leading rows are dropped*. The database has no shortcut — to return rows 300,001..300,020 it must produce and discard the 300,000 rows in front of them. Even with a perfect index on `(tenant_id, created_at, id)`, it walks 300,020 index entries and throws 300,000 away. Cost grows with the offset: **O(offset + limit)**. Page 1 touches 20 rows; page 15,001 touches 300,020. That is why the deep page is slow and the shallow page is not.

The second, subtler problem is **instability**. `OFFSET` is a blind count of rows to skip — it carries no memory of *what* you already saw. If someone inserts a row that sorts ahead of your window between fetching page 2 and page 3, every subsequent row shifts down by one: the last item of page 2 reappears as the first item of page 3 (a **duplicate**). A delete shifts the other way and an item is **skipped** entirely. Under any write traffic, offset paging silently lies.

## Keyset / seek method: O(limit), stable

Keyset pagination — Markus Winand's "No Offset," also called the *seek method* — replaces "skip N rows" with "start *after* the last row I saw." You remember the sort key of the last row returned and filter on it:

```sql
SELECT id, created_at, title
  FROM orders
 WHERE tenant_id = 42
   AND (created_at, id) < (:last_created_at, :last_id)   -- row-value comparison
 ORDER BY created_at DESC, id DESC
 LIMIT 20;
```

The row-value comparison `(created_at, id) < (:a, :b)` is the crux. Per the SQL standard, `X < Y` is true iff the leading components are equal and the first differing component is smaller — it evaluates left to right. That single predicate expresses "everything after this exact `(created_at, id)` pair" *without* needing the awkward `created_at < :a OR (created_at = :a AND id < :b)` expansion. PostgreSQL and modern MySQL both plan it as a range scan and use the composite index to **jump straight to the starting point** — no discarded rows. Cost is **O(limit)** regardless of how deep you are. Page 1 and page 15,001 do the same work.

It is also **stable**. The window is anchored to a value, not a position. Inserts and deletes ahead of your cursor change *where* the boundary lands in absolute terms but never cause you to re-see or skip a row relative to your last cursor — you always continue strictly after `(last_created_at, last_id)`.

Two hard requirements make this work:

1. **A total order.** The sort key must be unique or you must append a unique tie-breaker (the primary key). Paging on `created_at` alone breaks when two rows share a timestamp — the row-value comparison can straddle them and skip or repeat. Appending `id` guarantees determinism.
2. **The index must match the sort.** `(created_at DESC, id DESC)` in the query wants an index ordered the same way (or exactly reversible). Otherwise the planner sorts, and you are back to scanning.

## Building an opaque cursor

Never expose raw `(created_at, id)` in your API — clients will parse it, depend on it, and break when you change the sort. Encode the last row's keyset into an **opaque token**: serialize the tuple, base64 it, hand it back as `next_cursor`. It is a bookmark the client echoes, not data.

```python
import base64, json

def encode_cursor(sort_key: str, last_id: int) -> str:
    payload = {"k": sort_key, "i": last_id, "v": 1}  # v = schema version
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode()

def decode_cursor(token: str) -> tuple[str, int]:
    raw = base64.urlsafe_b64decode(token.encode())
    payload = json.loads(raw)
    if payload.get("v") != 1:
        raise ValueError("unsupported cursor version")
    return payload["k"], payload["i"]
```

The `v` field earns its keep: when you later change the sort order, you can reject stale cursors instead of returning garbage. For untrusted clients, sign the token (HMAC) so it cannot be forged into a probe of arbitrary key ranges. The request becomes `GET /orders?limit=20&cursor=eyJrIjoi...`; the response returns the page plus a fresh `next_cursor` built from the last row — or null when a page comes back short, meaning you have reached the end.

## Composite sort keys and deletions

**Composite keys** extend naturally — just widen the row value. Sorting by status, then priority, then id:

```sql
WHERE (status, priority, id) > (:last_status, :last_priority, :last_id)
ORDER BY status, priority, id
LIMIT 20;
```

Encode all three components into the cursor. The tie-breaker `id` stays last so the total order holds even when status and priority collide.

**Deletions are a non-issue** for keyset, which is one of its quiet wins. If the exact row your cursor points at gets deleted, the next query still works: `(created_at, id) < (:a, :b)` is a *range boundary*, not a lookup of a specific row. You continue from wherever that value would sit in the order, deleted or not. Offset paging, by contrast, treats a delete as a global shift and skips a live row.

## Cursor design for APIs

The industry has converged here. **Stripe** uses `starting_after` / `ending_before` parameters carrying an object id — a keyset cursor on the id order, returning `has_more` so clients know when to stop. **Relay's GraphQL Cursor Connections spec** formalizes the shape: a `Connection` has `edges`, each edge has a `node` and an opaque `cursor`, and `pageInfo` carries `hasNextPage`, `hasPreviousPage`, `startCursor`, and `endCursor`. Clients paginate with `first: N, after: <cursor>`. The spec is deliberate that a cursor is an *opaque string* — servers can encode whatever keyset they need and clients must not interpret it. That is exactly the base64 tuple above, dressed for GraphQL.

## The trade-off you must name in the interview

Keyset buys O(limit) and stability, but it gives up **random access**: there is no "jump to page 500." You can only go to the next (or previous, by reversing the comparison and the `ORDER BY`) page relative to a cursor you hold. You also lose a cheap exact page count. For infinite-scroll feeds, activity logs, and API list endpoints this is a non-issue — nobody deep-links page 15,001. When the product genuinely needs numbered pages over a small, mostly-static set, offset is fine; the rule of thumb is offset for shallow bounded lists, keyset for anything that grows or takes write traffic.

One more scale note: when a dataset outgrows a single node and is spread across shards (see [data partitioning and sharding](/articles/distributed-systems/2026-08-10-data-partitioning-sharding)), keyset cursors compose far better than offsets — each shard seeks by the same `(sort_key, id)` boundary and a merge picks the global next `limit`, whereas a global `OFFSET 300000` would force every shard to scan and discard, then coordinate the discard. The seek method keeps the per-shard work O(limit) too.

**Try next:** implement a `previous` page by flipping the comparison to `>` and the `ORDER BY` to ascending, then reversing the result set in the app — and add an HMAC signature to your cursor so a malicious client cannot craft one to walk another tenant's key range.
