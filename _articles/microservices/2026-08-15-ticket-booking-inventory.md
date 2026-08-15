---
title: "Ticket Booking Inventory: Holds, Oversells, and the Seat Race"
date: 2026-08-15
track: microservices
summary: "Two buyers, one seat, one instant — ticket booking is a concurrency problem in a product costume. This compares the three database answers (SELECT ... FOR UPDATE row locks, optimistic version checks, and the atomic UPDATE ... WHERE remaining > 0 conditional decrement), layers time-to-live seat holds and a payment saga on top, and covers the on-sale thundering herd: Cloudflare's Waiting Room admits users via Durable Object counters across a network spanning over 300 cities, and the Virtual Waiting Room on AWS solution issues queue positions and signed admission tokens so the arrival burst never reaches the inventory table."
reading_time: 7
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

**Gist.** Two buyers read "seat 14B: available" in the same instant and both attempt to buy; unless one write is ordered after the other and sees the first one's effect, the seat is oversold. The remedy is a single serialization point in the database — a row lock, a version predicate, or a conditional update — plus a time-to-live (TTL) *hold* so the buyer's payment happens outside any transaction. The cost is that every purchase funnels through one contended row, and the arrival rate of an on-sale must therefore be shaped by admission control before it reaches that row.

## Three ways to win the race

The invariant in all three cases is identical: **no seat transitions out of `available` twice**, and the transition is decided by a single atomic evaluation of a predicate against the current row.

**Pessimistic locking** takes the row lock before deciding:

```sql
BEGIN;
SELECT status FROM seats
 WHERE event_id = 917 AND seat_no = '14B'
   FOR UPDATE;                 -- second buyer blocks here
-- application checks status = 'available'
UPDATE seats SET status = 'held', hold_user = 42,
       hold_expires_at = now() + interval '10 minutes'
 WHERE event_id = 917 AND seat_no = '14B';
COMMIT;
```

Per the PostgreSQL explicit-locking documentation, `FOR UPDATE` blocks any concurrent `UPDATE`, `DELETE` or `SELECT ... FOR UPDATE` on that row until the holding transaction commits or rolls back. The losing transaction then unblocks, re-reads `status = 'held'`, and returns a definite "seat taken" rather than a guess. Two refinements matter: `NOWAIT` or `SKIP LOCKED` convert the wait into an immediate failure instead of parking a browser connection on a lock, and **the lock is held only inside the short transaction that creates the hold, never across the payment call**.

**Optimistic concurrency control** does not block; it detects. A `version` column is read without locks and the write is made conditional on it:

```sql
UPDATE seats SET status = 'held', hold_user = 42, version = version + 1
 WHERE event_id = 917 AND seat_no = '14B'
   AND status = 'available' AND version = 8;   -- version observed by the reader
-- rowcount 0 ⇒ another transaction won; re-read and report
```

Correctness is the same, and no writer waits. The trade is that **under contention the failure path is the common path**: each losing attempt costs a round trip and a retry. That is acceptable for reserved seating, where a given seat has a handful of simultaneous suitors, and poor for a single hot counter, where the conflict probability approaches one.

**Atomic conditional decrement** collapses read, check and write into one statement, which is the right shape for general-admission inventory where seats are fungible:

```sql
UPDATE ga_inventory SET remaining = remaining - 1
 WHERE event_id = 917 AND remaining > 0;
-- rowcount 1 ⇒ ticket granted; 0 ⇒ sold out
```

The database evaluates the predicate under its own row lock, so **`remaining` cannot go negative regardless of concurrency**. The same shape ports to Redis as a `DECR` guarded by a Lua script when the counter outgrows one PostgreSQL row; at that point the hot-row problem and its mitigations are those of [sharded counters](/articles/sys-patterns/2026-08-13-sharded-counters-hot-keys).

| Strategy | Blocks? | Contention behaviour | Best for |
|---|---|---|---|
| `FOR UPDATE` | Yes | Queues writers; deadlock risk with multi-seat carts (lock in sorted order) | Reserved seating, multi-row atomicity |
| Version check | No | Retry storms when hot | Low-conflict rows, ORM-friendly |
| Conditional `UPDATE` | Briefly | Serializes on one row — hot but correct | General-admission counters, quotas |

## Holds are leases, not locks

A buyer needs minutes to pay, and a database lock held for minutes exhausts the connection pool. A **hold** is therefore application state with a TTL — a lease expressed as `status = 'held'` plus `hold_expires_at`, on the order of minutes rather than seconds or hours. Expiry requires a mechanism, and there are two honest ones. **Lazy expiry** treats `held AND hold_expires_at < now()` as available in every read and every conditional write; it is cheap, but inventory returns to the pool only when some request happens to look at the row. **Active expiry** enqueues a delayed message at hold creation that fires at the expiry instant and releases the seat, as in [delayed messages and job scheduling](/articles/sys-patterns/2026-08-13-delayed-messages-job-scheduling). Systems commonly run both: the lazy predicate is the correctness backstop, and the delayed queue makes a displayed "2 tickets left" recover promptly.

If holds live in Redis for latency while seats live in PostgreSQL, the hold is a distributed lock and every caveat from [distributed locking and fencing tokens](/articles/sys-patterns/2026-08-11-distributed-locking-fencing-tokens) applies. A client can pause past its lease — a garbage-collection pause, a scheduler preemption — and resume believing it still owns the seat. The defence is that **the confirming write re-verifies the hold by carrying the hold identifier in its `WHERE` clause**, so the database, not the cache, remains the arbiter of ownership.

## Payment as a saga on top of the hold

Book-then-pay is a two-step distributed transaction with no two-phase commit available against a payment service provider, so it is an orchestrated [saga](/articles/microservices/2026-07-24-sagas-over-two-phase-commit): (1) create the hold, (2) authorize payment, (3) confirm the booking by flipping `held → sold` **with the hold identifier in the predicate**, (4) capture the funds. Compensations run backwards: a failed authorization releases the hold; a confirmation that fails because the hold expired mid-payment voids the authorization. Step 3's conditional write is what makes selling an expired-and-resold seat impossible even when the saga arrives late — the predicate no longer matches, the rowcount is zero, and the branch is the compensation rather than an oversell.

The booking application programming interface must itself be idempotent, because the buyer's client retries on timeout and a duplicate charge for one seat is the worst outcome available. The machinery is that of [idempotency keys](/articles/microservices/2026-07-30-idempotency-keys-safe-retries): a client-generated key on `POST /bookings`, a unique index over it, and replay of the original response on a repeat.

### Implementation sketch (Scala)

The hold state machine, with the fencing predicate that makes late confirmations safe:

```scala
enum SeatState:
  case Available
  case Held(holdId: UUID, expiresAt: Instant)
  case Sold(holdId: UUID)

/** Pure transition; the SQL predicates below encode exactly these guards. */
def confirm(state: SeatState, holdId: UUID, now: Instant): Either[String, SeatState] =
  state match
    case SeatState.Held(id, exp) if id == holdId && exp.isAfter(now) =>
      Right(SeatState.Sold(id))
    case SeatState.Held(id, _) if id == holdId => Left("hold expired")
    case _                                     => Left("seat not held by this hold")

// Both writes are single statements: the database evaluates the guard under its
// own row lock, so no application-side read-modify-write window exists.
val holdSql =
  """UPDATE seats SET status='held', hold_id=?, hold_expires_at=now() + interval '10 minutes'
      WHERE event_id=? AND seat_no=?
        AND (status='available' OR (status='held' AND hold_expires_at < now()))"""

val confirmSql =
  """UPDATE seats SET status='sold'
      WHERE event_id=? AND seat_no=?
        AND status='held' AND hold_id=? AND hold_expires_at >= now()"""

def tryHold(stmt: PreparedStatement): Boolean = stmt.executeUpdate() == 1  // 0 ⇒ lost the race
```

## The on-sale: waiting rooms for the thundering herd

None of the above survives an on-sale in which the arrival rate exceeds checkout throughput by orders of magnitude. The remedy is admission control *before* the booking system: a **virtual waiting room** that absorbs arrivals, orders them, and releases traffic at a rate the inventory tier can serialize. Cloudflare's Waiting Room performs this at the edge; its write-up describes the state-aggregation pipeline as built on **Durable Objects**, running on every server of a network spanning over 300 cities in more than 100 countries, with the per-data-centre counter check adding "usually less than 10 ms" while a cross-ocean call for shared slots costs around 60–70 ms. Capacity is split between slots reserved for a data centre by its historical share of traffic and a shared "Anywhere" pool that any data centre may draw from — in the write-up's worked example, San Jose is allotted 10% of 150 slots and 75% of the remainder forms the shared pool. The Virtual Waiting Room on AWS solution takes the token route: each arrival receives a queue position, an operator-advanced serving position determines who is admitted, and an admitted user exchanges the position for a signed, time-limited **JSON Web Token (JWT) admission token** that the booking API requires. SeatGeek's own build, described in the AWS Architecture Blog, is a custom DynamoDB-and-Lambda variant of the same idea: a token service issues access tokens matching available inventory and hands them out first-in-first-out or by customer status. The token check is load shedding with an audit trail: the herd waits in cheap infrastructure while the contended inventory row sees only a rate it can serialize.

## Pitfalls

- A multi-seat cart that locks rows in arrival order deadlocks against a cart requesting the same seats in the opposite order; PostgreSQL aborts one transaction with a deadlock error. Locking seats in a fixed sort order removes the cycle.
- Holding `FOR UPDATE` across the payment call keeps a database connection and a row lock open for the buyer's entire checkout; the pool drains and unrelated queries fail.
- Optimistic version checks on a general-admission counter turn into a retry storm: nearly every attempt reads a stale version, so throughput falls as concurrency rises.
- Lazy expiry alone leaves an abandoned hold occupying inventory indefinitely if no request reads that row, so displayed availability understates real availability.
- A confirmation that omits the hold identifier from its `WHERE` clause will happily sell a seat that expired and was re-held by another buyer, producing a double sale with no error.
- Trusting a Redis-held lease without re-verifying at commit time permits a paused client to confirm a seat whose lease has already passed to someone else.
- A booking endpoint without an idempotency key charges twice when the client retries a request that in fact succeeded but whose response was lost.
