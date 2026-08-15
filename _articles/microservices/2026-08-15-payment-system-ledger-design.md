---
title: "Design a Payment System: the Ledger Is the Hard Part"
date: 2026-08-15
track: microservices
summary: "A payment-system design question is graded on the ledger: an append-only double-entry log where every transfer writes balanced debit and credit entries and no posted row is ever UPDATEd. Stripe's Ledger and Square's Books both model money as immutable balanced transactions, and Modern Treasury's Accounting for Developers series explains why the debits = credits invariant is what stands between a system and silent money loss. This walks the schema, the transfer transaction, idempotent payment intents, payment-service-provider webhook retries, and reconciliation against processor reports."
reading_time: 7
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

**Gist.** Money state spread across service databases drifts, and there is no way to prove after the fact which record was right. The remedy is a **ledger**: an append-only double-entry log in which every movement is a set of entries whose debits equal credits, corrections are new compensating entries rather than mutations, and balances are derived from the log rather than stored beside it. The cost is write amplification and per-account serialization — every transfer takes a lock on each account it touches, so hot accounts become a throughput ceiling, and reads of a balance either scan entries or depend on a co-committed materialized row.

## Double-entry in one table pair

**Double-entry bookkeeping** rests on two rules. Every movement of money is recorded as at least two **entries** — a debit on one **account**, a credit on another — grouped into a transaction. Within every transaction, **debits must equal credits**. Modern Treasury's *Accounting for Developers* series states the consequence that matters to an engineer: accounts are not only customer wallets but also *internal* accounts (cash held at the processor, fees, payables), and the balancing rule means no code path creates or destroys money, only moves it between accounts. Square's **Books** service enforces this at the API boundary: a transaction is a set of entries that must sum to zero, and is rejected otherwise.

The second load-bearing property is **immutability**. Entries are append-only; a posted entry is never `UPDATE`d or `DELETE`d. A correction is a new, compensating entry — a reversal — which leaves the original visible to auditors and to reconciliation jobs. Stripe's Ledger write-up describes the payoff in verification terms: an immutable log of money movement can be replayed, balances re-derived from it, and any discrepancy against what banks and processors report raised as an alarm.

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

Amounts are integers in **minor units**. Binary floating point cannot represent most decimal fractions exactly, so repeated addition of float amounts accumulates residue that breaks the debits-equals-credits check.

## Never UPDATE a balance

The tempting schema is a `balance` column on `accounts` mutated by `UPDATE accounts SET balance = balance - 100`. That creates **two sources of truth** with no invariant tying them together: a process that crashes between the entry insert and the balance update, or a consumer that fires twice, leaves the column disagreeing with the log and produces no evidence of which is correct. The balance is instead **derived** — a `SUM` over entries — and where that scan is too slow, a **materialized balance** is maintained *inside the same database transaction* as the entries, serialized per account.

```sql
BEGIN;
-- serialize concurrent transfers touching these accounts
SELECT account_id FROM account_balances WHERE account_id IN (1, 2)
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

`FOR UPDATE` under a **consistent lock ordering by account id** gives per-account serialization and removes the deadlock cycle: two concurrent transfers touching accounts 1 and 2 acquire them in the same order, so neither holds a lock the other needs. The ordering has to hold for the order in which rows are locked, so the accounts are sorted before the statement runs rather than left to the planner. A periodic job re-derives balances from entries and reports any account where the materialized row and the derived sum differ. Airbnb's financial-reporting account describes the cost of not having a canonical log: financial figures were derived by transforming application tables that were designed for the product rather than for the books, which made reporting fragile as those tables changed.

## Idempotency and exactly-once effect

Payments travel over retrying networks, so every mutating endpoint carries an **idempotency key** — the `UNIQUE` constraint on `ledger_transactions.idempotency_key` is what enforces it. A retried capture collides with the unique index, the insert does not create a second transaction, and the caller receives the original result. Key scoping, response capture and expiry are covered in [idempotency keys and safe retries](/articles/microservices/2026-07-30-idempotency-keys-safe-retries); the ledger-specific point is that the key lives on the *transaction* row, which makes the **balanced entry set the atomic unit of deduplication** — a partial replay cannot post one side of a transfer. This is the general shape described in [exactly-once semantics](/articles/distributed-systems/2026-08-10-delivery-semantics-exactly-once): delivery is at-least-once, and the *effect* is once because the effect is an idempotent insert.

The same discipline applies to inbound events. A **payment service provider (PSP)** such as Stripe or Adyen delivers webhooks (`payment_intent.succeeded`, `charge.refunded`) at-least-once, possibly out of order, and retries over an extended window. The handler is therefore an idempotent projection: look up the event identifier, do nothing if it has been seen, otherwise post a ledger transaction keyed by that identifier and acknowledge. Webhook payload amounts are not authoritative — the object is re-fetched from the PSP application programming interface (API) before money is posted.

### Implementation sketch (Scala)

```scala
final case class Entry(accountId: Long, direction: "debit" | "credit", amountMinor: Long)

/** Rejects an unbalanced entry set before any row is written. */
def balanced(entries: List[Entry]): Boolean =
  val (d, c) = entries.partition(_.direction == "debit")
  d.map(_.amountMinor).sum == c.map(_.amountMinor).sum

def post(conn: java.sql.Connection, key: String, entries: List[Entry]): Long =
  require(balanced(entries), "debits must equal credits")
  conn.setAutoCommit(false)
  // lock ordering by account id is what prevents deadlock between concurrent transfers
  val lock = conn.prepareStatement(
    "SELECT account_id FROM account_balances WHERE account_id = ANY (?) ORDER BY account_id FOR UPDATE")
  val ids = entries.map(_.accountId).distinct.sorted
  lock.setArray(1, conn.createArrayOf("bigint", ids.map(id => java.lang.Long.valueOf(id)).toArray[Object]))
  lock.executeQuery()

  val tx = conn.prepareStatement(
    "INSERT INTO ledger_transactions (idempotency_key) VALUES (?) " +
    "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id")
  tx.setString(1, key)
  val rs = tx.executeQuery()
  if !rs.next() then                       // key already posted: replay, not a new transfer
    conn.rollback()
    existingTransactionId(conn, key)
  else
    val txId = rs.getLong(1)
    insertEntries(conn, txId, entries)     // ... unchanged
    applyMaterializedBalances(conn, entries)
    conn.commit()
    txId
```

## Reconciliation

A ledger records only what the system believes happened. **Reconciliation** compares it against what the processor and the bank report: the PSP's daily settlement report is ingested and each line matched to a ledger transaction on amount, currency and external identifier. Three buckets result — matched; present in the ledger but absent from the report (a payment recorded that never settled); present in the report but absent from the ledger (money moved that was never recorded). Stripe's Ledger write-up presents this checking as a continuous property of the system rather than a one-off audit: balances are re-derived from the immutable log and compared against what is reported externally, and a discrepancy is raised as an alarm. The operational form is an **unmatched-items queue** worked by humans, because the residue after automated matching is genuinely ambiguous.

## The payment flow is a saga

Authorization, capture and payout, with refund and void as compensations, form an orchestrated saga. Two-phase commit spanning an external PSP is not available, so the flow is a chain of local transactions with compensating transactions on failure (see [sagas over two-phase commit](/articles/microservices/2026-07-24-sagas-over-two-phase-commit)). Each saga step posts its own balanced ledger transaction, and downstream events such as receipts and analytics are published through the [transactional outbox](/articles/microservices/2026-07-26-transactional-outbox-pattern) in the same commit as the entries.

| Approach | Balance correctness | Auditability | Write cost | Failure mode |
|---|---|---|---|---|
| `UPDATE balance` column only | Drifts under retries and crashes | None — no history | Cheapest | Silent money loss |
| Append-only entries, derive balance | Consistent by construction | Full | `SUM` on read (cached or snapshotted) | Slow reads without a covering index |
| Entries + materialized balance, same transaction | Consistent if co-committed | Full | Row lock per account | Hot accounts serialize; splitting into subaccounts restores parallelism |

## Pitfalls

- **Floating-point amounts.** Totals that pass a spot check fail the global `SUM(debits) = SUM(credits)` audit, because binary floats cannot represent decimal fractions such as 0.10 exactly and the residue accumulates over many entries.
- **Locking accounts in arrival order.** Two transfers moving money in opposite directions between the same pair deadlock; one transaction is aborted by the database's deadlock detector. Ordering the `FOR UPDATE` by account id removes the cycle.
- **Materialized balance updated outside the entry transaction.** A crash between the two writes leaves the column and the log disagreeing with no record of which is authoritative; the periodic re-derivation is the only detector.
- **Idempotency key placed on the entry rather than the transaction.** A replay can post one side of a transfer, so the debits-equals-credits invariant fails for that transaction.
- **Trusting webhook payload amounts.** A replayed or reordered webhook posts a stale amount; re-fetching the object from the PSP API before posting is what avoids it.
- **Correcting a posted entry with `UPDATE`.** The audit trail no longer explains the balance, and reconciliation against an already-ingested settlement report finds a mismatch it cannot attribute.
- **Reconciliation matching on amount alone.** Two identical-value payments in the same window match each other's report lines, hiding a genuine unrecorded movement; matching includes the external identifier.
