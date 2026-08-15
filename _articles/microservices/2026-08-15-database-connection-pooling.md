---
title: "Connection Pooling: Why Postgres Wants Fewer Connections"
date: 2026-08-15
track: microservices
summary: "Postgres forks a process per connection, and Andres Freund's profiling shows thousands of idle connections cost measurable throughput before an extra query is issued. The remedy is aggressive pooling — but PgBouncer's transaction mode silently breaks SET, session advisory locks, and (until configured) prepared statements, and in a microservices fleet every service's 'small' pool multiplies against one max_connections."
reading_time: 7
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

**Gist.** A PostgreSQL connection is a forked operating-system **process**, and the per-transaction work of computing visibility scales with the number of connections whether or not they are executing anything — so idle connections consume throughput. A connection pool bounds the number of backends and makes surplus requests queue in the application instead of inside the database. The cost is that pooling reintroduces sharing: a proxy such as PgBouncer in transaction mode multiplexes clients onto server backends, which invalidates every construct that assumes session state persists on one connection.

## Why Postgres connections are expensive

Andres Freund's *Analyzing the Limits of Connection Scalability in Postgres* (2020) ranks the costs, and memory is the smallest of them: with reasonable settings a connection's overhead stays under roughly 2 MiB, so, in his words, "it is quite possible to have thousands of established connections."

The dominant cost is **snapshot scalability**. Every transaction must determine which other transactions' effects are visible to it, and that computation scales with the number of connections, **including connections that are idle**. Freund's profiling shows a busy server carrying thousands of idle connections spending a large share of its CPU inside `GetSnapshotData()`, with throughput falling well below the same server's idle-free result. PostgreSQL 14 shipped his snapshot-scalability work, which moves the degradation considerably further out; it does not remove it.

A further cost is the process-per-connection model itself: context switches and scheduler pressure that only a bounded number of active backends avoids.

The operational consequence: raising `max_connections` exchanges a queueing problem that is directly observable — threads waiting for a connection — for a throughput problem that is not, because the loss appears as CPU spent on bookkeeping rather than as an explicit wait.

## Pool sizing

The HikariCP wiki's *About Pool Sizing* page popularised the PostgreSQL project's starting-point formula:

```
pool_size = (core_count * 2) + effective_spindle_count
```

`core_count` excludes hyperthreads. `effective_spindle_count` is the number of concurrent input/output (I/O) operations the storage can service; it approaches 0 when the working set is fully cached, and on NVMe devices it effectively stops being a meaningful term.

The rationale on the [PostgreSQL wiki's Number Of Database Connections page](https://wiki.postgresql.org/wiki/Number_Of_Database_Connections) is that a query is either burning CPU, waiting on disk, or waiting on a lock. Once all three resources are saturated, additional concurrent transactions add only context switches, cache-line contention and `work_mem` pressure. The page's point is the counter-intuitive one that **a fixed batch of transactions finishes sooner when a queue holds concurrency down than when all of them are admitted at once** — the queueing is what shortens the total.

The HikariCP page's principal exhibit is an Oracle Real-World Performance demonstration in which reducing a pool from **2,048 connections to 96**, with no other change, moved response times "from ~100ms to ~2ms" — a 50-fold improvement. Its accompanying PostgreSQL benchmark shows transactions per second flattening around **50 connections**. The design target the page states is "a small pool, saturated with threads waiting for connections." A 16-core database server therefore suggests a pool of roughly 32 plus whatever the spindle term contributes — and that figure is **total across every client of that database**, not per process.

## PgBouncer: three modes

PgBouncer multiplexes many client connections onto a smaller set of server connections. `pool_mode` determines when a server connection becomes reusable.

| Mode | Server conn held for | Multiplexing win | Breaks |
|---|---|---|---|
| `session` | Entire client session | Low (absorbs idle clients only) | Nothing — all features work |
| `transaction` | One transaction | High — the usual choice | `SET`/`RESET`, session advisory locks, `LISTEN`, `WITH HOLD` cursors; prepared statements need config |
| `statement` | One statement | Highest | All of the above plus multi-statement transactions |

**The invariant transaction mode breaks is connection affinity.** A client's successive transactions may land on different server backends, and a given backend may carry state left by a different client. Every construct whose lifetime is the *session* rather than the *transaction* therefore misbehaves, and does so silently rather than by raising an error at the point of misuse:

- **`SET` / `RESET` at session level**: the PgBouncer documentation marks session-level `SET` as not supported in transaction mode. `SET LOCAL` inside a transaction is safe, because its scope ends with the transaction.
- **Session advisory locks** (`pg_advisory_lock`): the lock is held by whichever server backend served the call, and a later release may be issued on a different one. The transaction-scoped `pg_advisory_xact_lock` is released by transaction end and is therefore safe.
- **Prepared statements**: the classic failure is a driver preparing on one backend and executing on another, producing `prepared statement does not exist`. Current PgBouncer tracks protocol-level prepared statements when `max_prepared_statements` is non-zero, re-preparing them on whichever server connection a transaction lands on; the setting bounds how many are tracked per server connection. SQL-level `PREPARE` remains broken.
- **`LISTEN` and `WITH HOLD` cursors**: unsupported in transaction mode; such workloads require a separate session-mode pool.

A minimal production-shaped configuration:

```ini
; pgbouncer.ini
[databases]
appdb = host=10.0.0.5 port=5432 dbname=appdb

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
auth_type = scram-sha-256
pool_mode = transaction
max_client_conn = 2000        ; cheap: client side is a socket only
default_pool_size = 20        ; expensive: real Postgres backends per db/user
reserve_pool_size = 5         ; burst headroom after reserve_pool_timeout
max_prepared_statements = 200 ; non-zero enables protocol-level prepared stmts
server_reset_query = DISCARD ALL  ; used in session mode only
```

## Application-side pools and connection storms

HikariCP's guidance is a **fixed-size** pool: leaving `minimumIdle` unset makes it equal `maximumPoolSize` (default 10), so the pool does not attempt to create expensive connections at the moment it is already under load.

Three settings carry operational weight. `connectionTimeout` (default 30 s) bounds how long a thread waits for a connection and is the pool's backpressure signal. `maxLifetime` (default 30 minutes) must, per the HikariCP README, be "several seconds shorter than any database or infrastructure imposed connection time limit" — shorter than load-balancer or network-address-translation idle cutoffs and shorter than PgBouncer's `server_lifetime`. `keepaliveTime` exists to keep middleboxes from silently discarding idle TCP connections.

**Connection storms are the pool's characteristic failure mode.** A rolling deployment restarts many pods that each open their idle connections within the same few seconds; a primary failover causes every pool in the fleet to reconnect at once, presenting a freshly promoted primary with cold caches and a flood of logins, each of which forks a process. Mitigations are jittered retry with backoff, small fixed-size pools, and staggered retirement — HikariCP applies a minor negative attenuation to `maxLifetime` so that connections created together do not all retire, and reconnect, together.

## The microservices multiplication

Pool size is configured per **process**; `max_connections` is enforced per **database**. Twelve services × 10 pods × `maximumPoolSize=10` is 1,200 potential connections against a default `max_connections` of 100, with every team having sized its own pool defensibly. The arithmetic closes only through a shared funnel: a proxy in front of Postgres, small application pools, and a single owner of the global budget, such that `sum(all pools) ≤ pgbouncer default_pool_size ≤ what the formula supports on the hardware`. A database per service removes the multiplication entirely.

### Implementation sketch (Scala)

A fixed-size pool is a bounded queue plus a permit count. The load-bearing parts are that acquisition **times out rather than growing the pool**, and that a connection past its lifetime is discarded on return rather than mid-use.

```scala
import java.util.concurrent.{ArrayBlockingQueue, Semaphore, TimeUnit}

final class FixedPool(size: Int, maxLifetimeMs: Long, create: () => java.sql.Connection):
  private case class Entry(conn: java.sql.Connection, bornAt: Long)

  private val permits = Semaphore(size)                  // never exceeds `size` backends
  private val idle    = ArrayBlockingQueue[Entry](size)

  def withConnection[A](timeoutMs: Long)(body: java.sql.Connection => A): A =
    if !permits.tryAcquire(timeoutMs, TimeUnit.MILLISECONDS) then
      throw new java.sql.SQLTimeoutException(s"no connection within ${timeoutMs}ms")
    try
      val e = borrow()
      try body(e.conn)
      finally
        // retire on return, never while borrowed: a live statement must not lose its socket
        if alive(e) then idle.offer(e) else e.conn.close()
    finally permits.release()

  private def borrow(): Entry = Option(idle.poll()) match
    case Some(e) if alive(e) => e
    case Some(dead)          => dead.conn.close(); Entry(create(), now)
    case None                => Entry(create(), now)

  // negative attenuation: connections born together do not expire together
  private def alive(e: Entry): Boolean =
    val jitter = (e.conn.hashCode().abs % 30000).toLong
    now - e.bornAt < (maxLifetimeMs - jitter)

  private def now: Long = System.nanoTime() / 1000000
```

## Pitfalls

- **Session-level `SET` under `pool_mode = transaction`.** The statement succeeds, then a later transaction runs on a different server backend without the setting; the symptom is a `search_path` or `statement_timeout` that applies intermittently.
- **`pg_advisory_lock` under transaction mode.** The lock and its release can be issued on different backends, leaving a lock held for the life of a server connection. `pg_advisory_xact_lock` has no such exposure.
- **SQL-level `PREPARE`.** `max_prepared_statements` covers protocol-level prepared statements only; SQL `PREPARE` still yields `prepared statement does not exist`.
- **`maxLifetime` longer than an infrastructure idle timeout.** The middlebox drops the socket first, so the application discovers the dead connection as a failed query rather than the pool retiring it.
- **`minimumIdle` below `maximumPoolSize`.** The pool creates connections during the load spike that caused the demand, adding process forks and login cost at the worst moment.
- **Per-service pool sizing without a global owner.** Each pool is defensible in isolation; their sum exceeds `max_connections`, and the failure surfaces as login refusals during a deployment rather than as slow queries.
- **Raising `max_connections` to clear pool waits.** Idle connections still cost snapshot computation, so the visible queue disappears while throughput falls.
