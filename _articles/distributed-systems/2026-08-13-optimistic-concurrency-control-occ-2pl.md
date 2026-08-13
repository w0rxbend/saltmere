---
title: "OCC vs 2PL: bet on no conflict, or lock to be sure"
date: 2026-08-13
track: distributed-systems
summary: "Optimistic concurrency control's read-validate-write cycle versus pessimistic two-phase locking, why contention decides the winner, and how the same version/CAS trick shows up as SQL WHERE version = ? and HTTP If-Match at the API layer."
reading_time: 6
tags: [concurrency-control, occ, 2pl, compare-and-swap, etag, transactions]
sources:
  - title: "Kung & Robinson — On Optimistic Methods for Concurrency Control (ACM TODS, 1981)"
    url: "https://www.cs.cmu.edu/~dga/15-712/F07/lectures/12-optimism.pdf"
  - title: "Optimistic concurrency control (Wikipedia)"
    url: "https://en.wikipedia.org/wiki/Optimistic_concurrency_control"
  - title: "RFC 9110 §13.1.1 / §8.8 — If-Match, ETag, and conditional requests"
    url: "https://www.rfc-editor.org/rfc/rfc9110#name-if-match"
  - title: "HTTP conditional requests (MDN)"
    url: "https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Conditional_requests"
  - title: "Handling Optimistic Concurrency with ETags (Ed-Fi Alliance)"
    url: "https://docs.ed-fi.org/reference/data-exchange/api-guidelines/design-and-implementation-guidelines/api-implementation-guidelines/handling-optimistic-concurrency-with-etags/"
---

Two writers grab the same record. You have two philosophies for keeping them from clobbering each other. **Pessimistic** control assumes the conflict will happen, so it locks first and works second. **Optimistic** control assumes the conflict won't happen, works freely, and checks for a collision only at the end. The right choice is not a matter of taste — it's a function of how often those writers actually collide.

## 2PL: lock, work, release

Two-phase locking is the classic pessimistic protocol. A transaction acquires locks (shared for reads, exclusive for writes) in a **growing phase**, and once it releases *any* lock it enters the **shrinking phase** and may never acquire another. That discipline is what guarantees serializability. Correct, but it means a transaction *holds* locks for its whole duration, so other transactions block, lock-manager bookkeeping grows, and cycles of waiters produce **deadlocks** the system must detect and abort.

## OCC: read, validate, write

Kung & Robinson (1981) proposed skipping locks during execution entirely. A transaction runs in three phases:

1. **Read phase** — read freely and buffer all writes into a private workspace. Nothing is visible to others; nothing is locked.
2. **Validation phase** — before committing, check whether any concurrent transaction wrote data this one read. If someone did, the optimistic bet lost: **abort and retry**.
3. **Write phase** — no conflict, so atomically publish the buffered writes.

Validation typically uses **versions**: read a row's version alongside its data, and at commit confirm the version hasn't advanced. No locks are held during the (often long) read phase, so readers and writers don't block each other — the cost is paid only on conflict, as a retry.

## The version/CAS pattern in one UPDATE

You don't need a fancy engine to do OCC — a version column and a conditional write is the whole mechanism. This is **compare-and-swap** at the row level:

```sql
-- read
SELECT id, balance, version FROM accounts WHERE id = 42;   -- version = 7

-- ... application computes new balance ...

-- write, conditional on nothing having changed
UPDATE accounts
   SET balance = 90, version = version + 1
 WHERE id = 42 AND version = 7;
-- rows affected = 1  -> success
-- rows affected = 0  -> someone else won; re-read and retry
```

`rows affected = 0` *is* the validation failure. There's no lock between the `SELECT` and the `UPDATE`, so a concurrent writer that bumped `version` to 8 makes your `WHERE version = 7` match nothing. The application re-reads and retries. Under low contention this almost never fires; the fast path is completely lock-free.

## When each wins

| | OCC | 2PL |
|---|---|---|
| Assumes | conflicts rare | conflicts likely |
| Blocking | none during work | holds locks, others wait |
| Cost of conflict | wasted work + retry | wait, or deadlock abort |
| Failure mode | livelock/retry storms under high contention | deadlocks, lock convoys |
| Best for | read-heavy, low collision, short txns | write-heavy, hot rows, long txns |
| Overhead when idle | ~none | lock-manager bookkeeping |

The crossover is **contention**. At low conflict rates OCC's abort rate is negligible and you get lock-free throughput. As contention climbs, OCC transactions increasingly reach validation only to abort — work done, then thrown away — and can spiral into retry storms, while 2PL degrades more gracefully by simply serializing the hot row. Rule of thumb: optimistic for contended-rarely, pessimistic for contended-always.

## Same bet at the API layer: ETag + If-Match

The pattern isn't database-only. HTTP gives you OCC across stateless clients via conditional requests (RFC 9110). A `GET` returns an `ETag` — an opaque version token for that resource. The client echoes it back on write in `If-Match`; the server applies the change only if the current ETag still matches, else returns **412 Precondition Failed**.

```http
GET /accounts/42            -> 200  ETag: "v7"
PUT /accounts/42            If-Match: "v7"
   -> 200 (ETag now "v8")   if unchanged
   -> 412 Precondition Failed   if another writer already moved it to "v8"
```

A 412 is the HTTP version of `rows affected = 0`: re-fetch, reconcile, retry. Same three phases, same optimistic bet — just stretched across a network with no server-side lock held between read and write. Which is exactly why it scales for lost-update prevention on public APIs, where holding a lock across a client's think-time would be a denial-of-service waiting to happen.

**Try next:** add a `version integer` column, spin up two clients that read version 7 and both try the conditional `UPDATE`. Confirm exactly one gets `rows affected = 1`; make the loser re-read and retry, then crank concurrency until aborts dominate and you can see OCC's high-contention cliff.
