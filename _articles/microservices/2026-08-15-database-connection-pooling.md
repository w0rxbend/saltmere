---
title: "Connection Pooling: Why Your Postgres Wants Fewer Connections Than You Think"
date: 2026-08-15
track: microservices
summary: "Postgres forks a whole process per connection, and Andres Freund's profiling shows thousands of idle connections can halve throughput before you run a single extra query. The fix is aggressive pooling — but PgBouncer's transaction mode silently breaks SET, session advisory locks, and (until configured) prepared statements, and in a microservices fleet every service's 'small' pool multiplies against one max_connections."
reading_time: 6
tags: [postgres, connection-pooling, pgbouncer, hikaricp, databases, capacity]
sources:
  - title: "Andres Freund — Analyzing the Limits of Connection Scalability in Postgres (Citus Data / Microsoft)"
    url: "https://www.citusdata.com/blog/2020/10/08/analyzing-connection-scalability/"
  - title: "PostgreSQL Wiki — Number Of Database Connections"
    url: "https://wiki.postgresql.org/wiki/Number_Of_Database_Connections"
  - title: "HikariCP Wiki — About Pool Sizing"
    url: "https://github.com/brettwooldridge/HikariCP/wiki/About-Pool-Sizing"
  - title: "PgBouncer — Features (pooling modes and what they break)"
    url: "https://www.pgbouncer.org/features.html"
  - title: "PgBouncer — Configuration (pool_mode, default_pool_size, max_prepared_statements)"
    url: "https://www.pgbouncer.org/config.html"
---

Every Postgres connection is a forked OS **process** — not a thread, not a coroutine. That design decision from the 1980s is why connection pooling is non-negotiable at scale, and why the pool sizes that feel right are usually 10x too big.

## Why Postgres connections are expensive

Andres Freund's *Analyzing the Limits of Connection Scalability in Postgres* (2020) is the definitive breakdown, and it ranks the costs in a surprising order. Memory, the usual suspect, turns out to be the smallest problem: with sane settings a connection's overhead stays under ~2 MiB, so "it is quite possible to have thousands of established connections." The real killer is **snapshot scalability**: every transaction must compute which other transactions' effects it may see, and that work scales with the number of connections — *including idle ones*. His profiling shows a busy server with thousands of idle connections spending ~50% of CPU in `GetSnapshotData()`, with throughput down by more than half. (Postgres 14 shipped his fixes, which push the cliff out considerably — but the cliff is still there.) Third is the process-per-connection model itself: context switches and scheduling overhead that only a bounded worker count avoids.

The practical reading: idle connections are not free, so "just raise `max_connections`" trades a queueing problem you can see for a throughput problem you can't.

## Pool sizing: the formula and why small wins

The HikariCP wiki's *About Pool Sizing* page popularized the PostgreSQL project's starting-point formula:

```
pool_size = (core_count * 2) + effective_spindle_count
```

`core_count` excludes hyperthreads; `effective_spindle_count` counts how many concurrent I/Os your storage can service — approaching 0 when the working set is fully cached, and effectively vanishing as a term on modern NVMe. The origin is the [PostgreSQL wiki's Number Of Database Connections page](https://wiki.postgresql.org/wiki/Number_Of_Database_Connections), which explains the *why*: a query burns CPU, or waits on disk, or waits on locks — and beyond the point where all three are saturated, extra concurrent transactions only add context switches, cache-line contention, and `work_mem` pressure. Their benchmark point: 10,000 transactions finish *sooner* run 5–20 at a time than 500 at a time.

The HikariCP page's exhibit A is an Oracle Real-World Performance demo: dropping a pool from 2,048 connections to 96 — no other change — took response times "from ~100ms to ~2ms," a 50x improvement, and their Postgres benchmark shows TPS flattening around 50 connections. The design goal it lands on: "you want a small pool, saturated with threads waiting for connections." A 16-core DB server suggests a pool in the 30s — *total, across everything talking to it*. When requests exceed that, they should queue in the pool (cheap) rather than inside Postgres (expensive).

## PgBouncer: three modes, three sets of gotchas

PgBouncer multiplexes many client connections onto few server connections. The `pool_mode` decides when a server connection can be reused:

| Mode | Server conn held for | Multiplexing win | Breaks |
|---|---|---|---|
| `session` | Entire client session | Low (only absorbs idle clients) | Nothing — all features work |
| `transaction` | One transaction | High — the usual choice | `SET`/`RESET`, session advisory locks, `LISTEN`, `WITH HOLD` cursors; prepared statements need config |
| `statement` | One statement | Highest | All of the above plus multi-statement transactions |

Transaction mode is where the value and the pain both live. Anything that assumes *session* state sticks to *your* connection silently misbehaves, because your next transaction may run on a different server connection carrying someone else's state — or none:

- **`SET` / `SET LOCAL` outside a transaction**: the pgbouncer docs mark session-level `SET` as never supported in transaction mode. `SET LOCAL` inside a transaction is fine.
- **Session advisory locks** (`pg_advisory_lock`): the lock belongs to whichever server connection you happened to use; use transaction-scoped `pg_advisory_xact_lock` instead.
- **Prepared statements**: historically the classic footgun (drivers prepare on one connection, execute on another → "prepared statement does not exist"). Modern PgBouncer tracks protocol-level prepared statements when `max_prepared_statements` is non-zero (current docs default it to 200, with an LRU cache per server connection) — but SQL-level `PREPARE` still breaks.
- **`LISTEN`** and **`WITH HOLD` cursors**: never in transaction mode; route those workloads through a session-mode pool.

A minimal production-shaped config:

```ini
; pgbouncer.ini
[databases]
appdb = host=10.0.0.5 port=5432 dbname=appdb

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
auth_type = scram-sha-256
pool_mode = transaction
max_client_conn = 2000        ; cheap: clients are just sockets here
default_pool_size = 20        ; expensive: real Postgres backends per db/user
reserve_pool_size = 5         ; burst headroom after reserve_pool_timeout
max_prepared_statements = 200 ; protocol-level prepared stmt support
server_reset_query = DISCARD ALL  ; only used in session mode
```

## HikariCP knobs and connection storms

On the application side, HikariCP's own guidance is to run a **fixed-size** pool: leave `minimumIdle` unset so it equals `maximumPoolSize` (default 10), because a pool that grows under load is adding expensive connections at the worst moment. The knobs that matter operationally: `connectionTimeout` (default 30 s — how long a thread queues for a connection; this is your backpressure signal), `maxLifetime` (default 30 min — the README insists it be "several seconds shorter than any database or infrastructure imposed connection time limit," i.e. shorter than your LB/NAT idle cutoff and PgBouncer's `server_lifetime`), and `keepaliveTime` to stop middleboxes silently dropping idle TCP.

**Connection storms** are the pool pattern's failure mode. A rolling deploy restarts 40 pods that each open `minimumIdle` connections in the same seconds; a primary failover makes every pool in the fleet reconnect simultaneously — hitting a fresh primary with cold caches and a login flood (each login forking a process). Mitigations: connect with jittered retries and backoff, keep pools fixed-size and small, and note HikariCP deliberately applies "minor negative attenuation" to `maxLifetime` so all connections don't retire — and reconnect — at once.

## The microservices multiplication

The quiet capacity bug: pool sizing is per-*process*, but `max_connections` is per-*database*. Twelve services × 10 pods × `maximumPoolSize=10` is 1,200 potential connections against a default `max_connections` of 100 — and each service team sized "their" pool reasonably. The arithmetic only works with a shared funnel: PgBouncer (or RDS Proxy et al.) in front of Postgres, application pools kept small, and one owner for the *global* budget: `sum(all pools) ≤ pgbouncer default_pool_size ≤ what the formula says the hardware supports`. Per-service databases (the microservices ideal) reset the multiplication — which is one more argument for not sharing one Postgres among twelve services.

**Try next:** run `pgbench -c 10`, `-c 50`, `-c 300` against one Postgres instance and plot TPS and p99 — then put PgBouncer (transaction mode, `default_pool_size=20`) in front of the 300-client run and watch it match the 50-client numbers.
