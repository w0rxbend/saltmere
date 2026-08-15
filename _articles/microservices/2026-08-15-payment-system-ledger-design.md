---
title: "Design a Payment System: the Ledger Is the Hard Part"
date: 2026-08-15
track: microservices
summary: "Interviewers ask for 'a payment system' but grade you on the ledger: an append-only double-entry log where every transfer writes balanced debit/credit entries and no row is ever UPDATEd. Stripe's Ledger (2024) and Square's Books both model money as immutable balanced transactions, and Modern Treasury's Accounting for Developers series explains why the debits = credits invariant is the only thing standing between you and silent money loss. This walks the schema, the transfer transaction, idempotent payment intents, PSP webhook retries, and reconciliation against processor reports."
reading_time: 6
tags: [payments, ledger, double-entry, idempotency, reconciliation, sagas, postgres]
sources:
  - title: "Ilya Ganelin — Ledger: Stripe's system for tracking and validating money movement"
    url: "https://stripe.dev/blog/ledger-stripe-system-for-tracking-and-validating-money-movement"
  - title: "Square Developer Blog — Books, an immutable double-entry accounting database service"
    url: "https://developer.squareup.com/blog/books-an-immutable-double-entry-accounting-database-service/"
  - title: "Modern Treasury — Accounting for Developers, Part I"
    url: "https://www.moderntreasury.com/journal/accounting-for-developers-part-i"
  - title: "Alice Liang — Tracking the Money: Scaling Financial Reporting at Airbnb"
    url: "https://medium.com/airbnb-engineering/tracking-the-money-scaling-financial-reporting-at-airbnb-6d742b80f040"
---

The interview prompt says "design a payment system," and most candidates spend forty minutes on API gateways and card networks. The part that actually distinguishes a pass from a strong hire is the **ledger** — the datastore that answers "where is the money, and can you prove it?" Stripe, Square, and Airbnb each independently rebuilt this layer around the same 500-year-old idea, and each wrote up why: money state scattered across service databases drifts, and drift in money is an incident, not a bug.

## Double-entry in one table pair

**Double-entry bookkeeping** is two rules. First, every movement of money is recorded as at least two **entries** — a debit on one **account**, a credit on another — grouped into a transaction. Second, within every transaction, **debits must equal credits**. Modern Treasury's *Accounting for Developers* series is the best engineer-oriented explanation: accounts aren't just user wallets, they're also *internal* accounts (cash-at-PSP, fees, payables), and the balancing rule means money is never created or destroyed by a code path — only moved. Square's **Books** service enforces exactly this at the API layer: an immutable service where "transactions" are sets of entries that must sum to zero, rejected otherwise.

The second load-bearing property is **immutability**. Entries are append-only; you never `UPDATE` or `DELETE` a posted entry. Corrections are new, compensating entries (a reversal), which preserves the audit trail regulators and your own reconciliation jobs depend on. Stripe's Ledger write-up frames the payoff: an immutable log of money movement is something you can *verify* — replay it, re-derive balances, and alarm on any discrepancy between what the ledger says and what banks and processors report.

```sql
CREATE TABLE accounts (
  id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name       TEXT NOT NULL,            -- 'user:42:wallet', 'platform:stripe_cash'
  currency   CHAR(3) NOT NULL,
  type       TEXT NOT NULL CHECK (type IN ('asset','liability','revenue','expense'))
);

CREATE TABLE ledger_transactions (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  effective_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  metadata        JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE entries (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  transaction_id BIGINT NOT NULL REFERENCES ledger_transactions(id),
  account_id     BIGINT NOT NULL REFERENCES accounts(id),
  direction      TEXT NOT NULL CHECK (direction IN ('debit','credit')),
  amount_minor   BIGINT NOT NULL CHECK (amount_minor > 0)  -- cents; never floats
);
```

Amounts are integers in minor units — floating point in a ledger is an automatic interview fail.

## Never UPDATE a balance

The tempting schema is `accounts.balance` plus `UPDATE accounts SET balance = balance - 100`. Now you have two sources of truth, and the first crashed process or double-fired consumer desynchronizes them silently. The correct answer: the balance is **derived** — `SUM` over entries — and if that's too slow, you maintain a **materialized balance** that is updated *in the same database transaction* as the entries, serialized per account:

```sql
BEGIN;
-- serialize concurrent transfers touching these accounts
SELECT id FROM account_balances WHERE account_id IN (1, 2)
  ORDER BY account_id FOR UPDATE;

INSERT INTO ledger_transactions (idempotency_key) VALUES ('pi_9f3a:capture')
  RETURNING id;                                       -- say 7001

INSERT INTO entries (transaction_id, account_id, direction, amount_minor) VALUES
  (7001, 1, 'debit',  2500),   -- user wallet down $25.00
  (7001, 2, 'credit', 2500);   -- merchant payable up $25.00

UPDATE account_balances SET balance_minor = balance_minor - 2500 WHERE account_id = 1;
UPDATE account_balances SET balance_minor = balance_minor + 2500 WHERE account_id = 2;
COMMIT;
```

The `ORDER BY ... FOR UPDATE` (consistent lock ordering avoids deadlocks) gives you per-account serialization; a nightly job re-derives balances from entries and screams if they differ. Airbnb's financial-reporting rebuild is the cautionary tale for skipping this: computing financials by transforming application tables after the fact meant every schema change upstream threatened the books, which is why they moved to emitting canonical financial events instead.

## Idempotency and exactly-once effect

Payments run over retrying networks, so every mutating endpoint takes an **idempotency key** — that's the `UNIQUE` constraint on `ledger_transactions.idempotency_key` above doing the real work. A retried capture hits the unique index, the insert no-ops, and the client gets the original result back. The mechanics (key scoping, response capture, TTLs) are in [idempotency keys and safe retries](/articles/microservices/2026-07-30-idempotency-keys-safe-retries); the ledger-specific point is that the key lives on the *transaction* row, making the balanced entry-set the atomic unit of deduplication. This is the general pattern from [exactly-once semantics](/articles/microservices/2026-08-13-exactly-once-delivery-semantics-kafka): delivery is at-least-once, but the *effect* is exactly-once because the effect is an idempotent insert.

The same discipline applies inbound. Your **PSP** (Stripe, Adyen) delivers webhooks (`payment_intent.succeeded`, `charge.refunded`) at-least-once, out of order, for up to days of retries. The handler must be a pure idempotent projection: look up the event ID, if seen do nothing, else record the ledger transaction keyed by `event_id` and ack. Never trust webhook payload amounts alone — re-fetch the object from the PSP API before posting money.

## Reconciliation: trust but verify

Even a perfect ledger only records what *you think* happened. **Reconciliation** compares it against what the processor and bank say happened: ingest the PSP's daily settlement report and match every line to a ledger transaction on (amount, currency, external ID). Three buckets fall out — matched, in-ledger-but-not-in-report (did we record a payment that never settled?), in-report-but-not-in-ledger (did money move that we never recorded?). Stripe's Ledger runs this continuously rather than nightly, turning discrepancy count into an SLO-style metric with owning teams paged on regressions. In an interview, saying "reconciliation job against processor reports, with an unmatched-items queue worked by humans" signals you've seen real money systems.

## The payment flow is a saga

Auth → capture → payout, with refund and void as compensations, is a textbook orchestrated saga — no 2PC with your PSP is possible, so you chain local transactions and compensate on failure (see [sagas over two-phase commit](/articles/microservices/2026-07-24-sagas-over-two-phase-commit)). Each saga step posts its own balanced ledger transaction, and downstream events (receipt emails, analytics) are published via the [transactional outbox](/articles/microservices/2026-07-26-transactional-outbox-pattern) in the same commit as the entries.

| Approach | Balance correctness | Auditability | Write cost | Failure mode |
|---|---|---|---|---|
| `UPDATE balance` column only | Drifts under retries/crashes | None — no history | Cheapest | Silent money loss |
| Append-only entries, derive balance | Always consistent | Full | `SUM` on read (cache/snapshot it) | Slow reads if unindexed |
| Entries + materialized balance, same txn | Consistent if co-committed | Full | Row lock per account | Hot accounts serialize (shard them into subaccounts) |

## What to say in the interview

Lead with the invariants: append-only entries, debits = credits enforced per transaction, integer minor units, idempotency key on the transaction, balances derived or co-committed under a per-account lock. Then the boundary: PSP webhooks are at-least-once inputs to an idempotent projection; reconciliation against settlement reports is the detection layer; the auth/capture/refund flow is a saga whose every step is a balanced ledger write. That's the whole system — everything else is queues and HTTP.

**Try next:** build the three-table schema above in Postgres, write the transfer transaction as a stored function taking an idempotency key, then hammer it with 50 concurrent clients retrying the same 10 keys — verify afterward that `SUM(debits) = SUM(credits)` globally and that every materialized balance equals its entry-derived balance.
