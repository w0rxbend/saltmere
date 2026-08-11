---
title: "Keyset pagination vs OFFSET: the seek method for lists at scale"
date: 2026-08-11
track: microservices
summary: "Why OFFSET n LIMIT m rots on deep pages and drifts when rows change under it, and how keyset (seek) pagination with a composite sort key, a matching index, and opaque cursors fixes both. With SQL and a Relay-style connection API."
reading_time: 6
tags: [pagination, keyset, cursor, sql, api-design, postgres]
sources:
  - title: "Markus Winand — We need tool support for keyset pagination (No Offset)"
    url: "https://use-the-index-luke.com/no-offset"
  - title: "Markus Winand — OFFSET is bad for skipping previous rows"
    url: "https://use-the-index-luke.com/sql/partial-results/fetch-next-page"
  - title: "GraphQL Cursor Connections Specification (Relay)"
    url: "https://relay.dev/graphql/connections.htm"
  - title: "How pagination works — Stripe API Reference"
    url: "https://docs.stripe.com/api/pagination"
  - title: "Pagination — Slack Developer Docs"
    url: "https://docs.slack.dev/apis/web-api/pagination/"
---

Almost every list endpoint ships with `OFFSET n LIMIT m` because it maps cleanly onto "page 3, 20 per page." It works in the demo and then quietly becomes the slowest query in your service once the table has a few million rows and someone builds an infinite-scroll feed on it. This is a staple of both API-design reviews and system-design interviews, and the fix — keyset, also called seek or cursor pagination — is worth knowing cold.

## Why OFFSET degrades

The problem is in the semantics. `OFFSET 100000 LIMIT 20` does not tell the database to jump to the hundred-thousandth row. It tells it to produce rows in order, count off the first hundred thousand, throw them away, and return the next twenty. As Markus Winand puts it, "the database must still fetch these rows from the disk and bring them in order before it can send the following ones." Deep pages are therefore O(n) in the offset: page 1 is instant, page 5000 reads and discards everything before it. Latency grows linearly with how far the user scrolls, and the work is pure waste.

There is a second, subtler failure a good reviewer will raise: **OFFSET is not stable under writes.** It counts positions in a result set being mutated concurrently. Insert a row near the top between loading page 1 and requesting page 2, and every later row shifts down by one — so the last row of page 1 reappears as the first row of page 2 (a duplicate); a delete shifts the other way and a row gets silently skipped. The offset remembers a count, and that count means nothing once the data has moved.

## Keyset: filter, don't skip

Keyset pagination throws away the row counter and remembers a *value* instead — the sort key of the last row you saw. The next page is not "skip 20 more" but "everything ordered after this specific row." Because you filter on an indexed column rather than counting positions, the database seeks straight to the starting point and reads exactly `m` rows. Every page costs the same.

The one catch is that ordering must be **total**. Sort by `created_at` alone and colliding timestamps make a boundary row ambiguous, so you skip or repeat at page edges. Append a unique tie-breaker — the primary key — and compare the pair as a tuple:

```sql
-- First page
SELECT id, created_at, title
  FROM articles
 ORDER BY created_at DESC, id DESC
 LIMIT 20;

-- Next page: pass the last row's (created_at, id) back in
SELECT id, created_at, title
  FROM articles
 WHERE (created_at, id) < (:last_created_at, :last_id)
 ORDER BY created_at DESC, id DESC
 LIMIT 20;
```

The row-value comparison `(created_at, id) < (:c, :id)` is real SQL (Postgres, MySQL 8+, and others support it) and means exactly "ordered strictly after this row" for the matching `ORDER BY`. If your database or ORM won't emit tuple comparison, the logically equivalent expansion is `created_at < :c OR (created_at = :c AND id < :id)`.

This only performs if the sort key is backed by a matching composite index in the same order and direction:

```sql
CREATE INDEX idx_articles_seek
    ON articles (created_at DESC, id DESC);
```

Now the planner does an index range scan starting at the boundary tuple — no discarded rows, no full sort, constant cost per page. And because the boundary is a concrete row rather than a position, concurrent inserts and deletes above it no longer shift anything: you never see the same row twice and never skip one.

| | OFFSET / LIMIT | Keyset / seek |
| --- | --- | --- |
| Deep-page cost | O(offset) — scans and discards | O(1) per page — index seek |
| Stable under writes | No — skips / duplicates rows | Yes — boundary is a real row |
| Jump to arbitrary page N | Yes | No (its main limitation) |
| Needs supporting index | Helps the sort only | Required, must match ORDER BY |
| Good for | Small admin tables, page pickers | Feeds, infinite scroll, API export |

## Opaque cursors: don't leak the sort key

You could return `?last_created_at=2026-08-11T09:00:00Z&last_id=48210` to the client, but now the pagination contract is part of your public API. Change the sort key and every stored cursor breaks. The standard move is to treat the cursor as an **opaque token**: serialize the boundary tuple and base64-encode it, so clients pass it back verbatim without parsing it.

```python
import base64, json

def encode_cursor(row) -> str:
    payload = {"c": row["created_at"].isoformat(), "id": row["id"]}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()

def decode_cursor(token: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(token))
```

This is what the big API vendors do. Stripe's list endpoints take a `starting_after` (and `ending_before`) object-ID cursor plus a `limit`, and return `has_more` so the client knows when to stop — no page numbers anywhere. Slack's Web API returns a `response_metadata.next_cursor` string you feed into the next request's `cursor` param, and hands back an empty string at the end. The client never learns what's inside the token, which is the point: you keep the freedom to change the underlying key.

## Exposing it in a REST or GraphQL API

For GraphQL, the **Relay Cursor Connections** spec formalizes this. A connection returns `edges`, each with a `node` and an opaque `cursor`, plus a `pageInfo` object carrying `hasNextPage`, `hasPreviousPage`, `startCursor`, and `endCursor`. Clients paginate with `first: 20, after: $endCursor`:

```graphql
{
  articles(first: 20, after: "eyJjIjoiMjAyNi0wOC0xMSIsImlkIjo0ODIxMH0") {
    edges { node { id title } cursor }
    pageInfo { hasNextPage endCursor }
  }
}
```

A REST equivalent is a small envelope: `{ "data": [...], "next_cursor": "...", "has_more": true }`. For backward paging, either keep a separate `ending_before`-style cursor as Stripe does, or flip the comparison operator and `ORDER BY` direction, then reverse the resulting slice before returning it.

## The honest trade-off

Keyset has one real cost, and interviewers will look for whether you name it: **you cannot jump to an arbitrary page N.** There is no "go to page 500" because you only know how to continue from a row you've already seen. That rules it out for classic numbered page pickers, but it's a perfect fit for feeds, timelines, infinite scroll, and machine consumers exporting a dataset — none of which need random access.

The related trap is total counts. `SELECT count(*)` over a large filtered set is itself an expensive scan and gets stale the instant it returns, so a precise "Page 3 of 8,412" is costly at scale. Prefer a `has_more` boolean (fetch `m + 1` rows; if you got the extra one, there's a next page) or an approximate count from table statistics, and reserve exact totals for small tables where they're cheap.

**Try next:** Take one OFFSET endpoint in your service, add a `(sort_col, id)` composite index, and reimplement it as a keyset query returning a base64 `next_cursor` and `has_more`; then run `EXPLAIN ANALYZE` on page 1 versus page 5000 for both versions and watch the OFFSET plan's row count climb while the keyset plan stays flat.
