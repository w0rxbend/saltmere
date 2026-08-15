---
title: "Design Ticket Booking: Holds, Oversells, and the Inventory Race"
date: 2026-08-15
track: microservices
summary: "Two buyers, one seat, one moment — ticket booking is a concurrency problem wearing a product costume. This compares the three database answers (SELECT ... FOR UPDATE row locks, optimistic version checks, and the atomic UPDATE ... WHERE remaining > 0 conditional decrement), layers TTL-based seat holds and a payment saga on top, and covers the on-sale thundering herd: Cloudflare's Waiting Room admits users via Durable Object counters across 300+ cities, and AWS's Virtual Waiting Room solution (used by SeatGeek) issues queue positions and signed admission tokens so 100k fans don't hit your inventory table at once."
reading_time: 6
tags: [ticket-booking, inventory, row-locking, optimistic-concurrency, seat-holds, waiting-room, sagas]
sources:
  - title: "PostgreSQL Documentation — 13.3. Explicit Locking (row-level locks)"
    url: "https://www.postgresql.org/docs/current/explicit-locking.html"
  - title: "AWS Architecture Blog — Build a Virtual Waiting Room with Amazon DynamoDB and AWS Lambda at SeatGeek"
    url: "https://aws.amazon.com/blogs/architecture/build-a-virtual-waiting-room-with-amazon-dynamodb-and-aws-lambda-at-seatgeek/"
  - title: "Cloudflare Blog — How Waiting Room makes queueing decisions on Cloudflare's highly distributed network"
    url: "https://blog.cloudflare.com/how-waiting-room-queues/"
  - title: "Virtual Waiting Room on AWS — How the solution works"
    url: "https://docs.aws.amazon.com/solutions/latest/virtual-waiting-room-on-aws/how-the-solution-works.html"
---

Strip away seat maps and payment forms and ticket booking is one race: two buyers read "seat 14B: available" at the same instant, both click buy, and only the database decides who actually gets it. If your design lets both succeed you've oversold; if it makes one buyer wait behind a lock held across a 30-second checkout you've built a queue with extra steps. The interview is about where you put the serialization point — and how you survive the moment 100,000 people arrive at once.

## Three ways to win the race

**Pessimistic locking** takes the row lock before deciding:

```sql
BEGIN;
SELECT status FROM seats
 WHERE event_id = 917 AND seat_no = '14B'
   FOR UPDATE;                 -- second buyer blocks here
-- app checks status = 'available'
UPDATE seats SET status = 'held', hold_user = 42,
       hold_expires_at = now() + interval '10 minutes'
 WHERE event_id = 917 AND seat_no = '14B';
COMMIT;
```

Postgres's `FOR UPDATE` (per the explicit-locking docs) blocks any concurrent `UPDATE`, `DELETE`, or `SELECT ... FOR UPDATE` on that row until commit. The loser unblocks, re-reads `status = 'held'`, and gets a clean "seat taken." Two refinements matter in practice: `NOWAIT` or `SKIP LOCKED` to fail fast instead of queueing browsers on a lock, and *lock only inside a short transaction that creates the hold* — never across the payment call.

**Optimistic concurrency** doesn't block; it detects. Add a `version` column, read without locks, and make the write conditional:

```sql
UPDATE seats SET status = 'held', hold_user = 42, version = version + 1
 WHERE event_id = 917 AND seat_no = '14B'
   AND status = 'available' AND version = 8;   -- version you read
-- rowcount 0 ⇒ somebody beat you; re-read and tell the user
```

Same correctness, no lock waits, but under real contention most attempts fail and retry — fine for seat-map booking where each seat has only a handful of simultaneous suitors, bad for a single hot counter.

**Atomic conditional decrement** collapses read-check-write into one statement, and is the right shape for general-admission inventory where seats are fungible:

```sql
UPDATE ga_inventory SET remaining = remaining - 1
 WHERE event_id = 917 AND remaining > 0;
-- rowcount 1 ⇒ you got a ticket; 0 ⇒ sold out
```

The database evaluates the predicate under its own row lock, so `remaining` can never go negative. This is also the pattern to port to Redis (`DECR` guarded by a Lua script) if the counter outgrows one Postgres row — at which point the hot-row problem and its mitigations look exactly like [sharded counters](/articles/sys-patterns/2026-08-13-sharded-counters-hot-keys).

| Strategy | Blocks? | Contention behavior | Best for |
|---|---|---|---|
| `FOR UPDATE` | Yes | Queues writers; deadlock risk with multi-seat carts (lock in sorted order) | Reserved seating, multi-row atomicity |
| Version check | No | Retry storms when hot | Low-conflict rows, ORM-friendly |
| Conditional `UPDATE` | Briefly | Serializes on one row — hot but correct | GA counters, quotas |

## Holds are leases, not locks

Buyers need minutes to pay, and holding a database lock for minutes is how you get connection-pool exhaustion. So a **hold** is application state with a TTL — a lease: `status = 'held'`, `hold_expires_at`, typically 5–10 minutes. Expiry needs a mechanism, and you have two honest options: **lazy expiry** (every read and every conditional write treats `held AND hold_expires_at < now()` as available — cheap, but inventory "returns" only when someone looks) or an **active expiry queue** — enqueue a delayed message at hold-creation that fires at expiry and releases the seat, per [delayed messages and job scheduling](/articles/sys-patterns/2026-08-13-delayed-messages-job-scheduling). Production systems do both: lazy as correctness backstop, delayed queue so "2 tickets left" recovers promptly.

If holds live in Redis for speed while seats live in Postgres, you've built a distributed lock, and every caveat from [distributed locking and fencing tokens](/articles/sys-patterns/2026-08-11-distributed-locking-fencing-tokens) applies: a client can pause past its lease and wake up believing it still holds the seat, so the *confirming* write must re-verify the hold (hold ID as fencing token in the `WHERE` clause) — the database stays the arbiter.

## Payment as a saga on top of the hold

Book-then-pay is a two-step distributed transaction with no 2PC available against your PSP, so it's an orchestrated [saga](/articles/microservices/2026-07-24-sagas-over-two-phase-commit): (1) create hold, (2) authorize payment, (3) confirm booking — flip `held → sold` *with the hold ID in the predicate*, (4) capture. Compensations run backward: auth fails ⇒ release hold; confirm fails because the hold expired mid-payment ⇒ void the auth and apologize. Step 3's conditional write is what makes an expired-and-resold seat impossible even when the saga limps in late.

The booking API itself must be idempotent — the buyer's client retries on timeout, and "charged twice for one seat" is the worst headline available. Same machinery as [idempotency keys](/articles/microservices/2026-07-30-idempotency-keys-safe-retries): client-generated key on `POST /bookings`, unique index, replay the original response.

## The on-sale: waiting rooms for the thundering herd

None of the above survives first contact with a Taylor-Swift-scale on-sale, where the arrival rate is 1000x your checkout throughput. The fix is admission control *before* the booking system: a **virtual waiting room** that absorbs arrivals, orders them, and leaks traffic at a rate your inventory tier can sustain. Cloudflare's Waiting Room does this at the edge — its write-up describes admission counters as **Durable Objects** synchronizing workers per data center (~10 ms added locally) across 300+ cities, with capacity reserved by geography and a shared "Anywhere" pool (~75% of slots) so no region starves. AWS's Virtual Waiting Room solution — the pattern SeatGeek built on DynamoDB and Lambda — makes the mechanics explicit: each arrival gets a monotonically increasing queue position, a serving counter advances at your chosen rate, and admitted users exchange their position for a signed, time-limited **JWT admission token** that the booking API requires. That token check is load-shedding with receipts: the herd waits in cheap infrastructure while your Postgres row locks only ever see a trickle they can serialize.

**Try next:** create a `seats` table with 100 rows and hammer it from 200 concurrent workers using each strategy in turn — `FOR UPDATE`, `FOR UPDATE SKIP LOCKED`, version check, conditional `UPDATE` — and compare success latency, retry counts, and (the only hard requirement) that exactly 100 bookings succeed every time.
