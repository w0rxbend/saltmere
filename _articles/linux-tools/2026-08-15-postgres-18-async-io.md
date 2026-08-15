---
title: "Postgres 18's Async I/O: io_uring Reaches Your Read Path"
date: 2026-08-15
track: linux-tools
summary: "PostgreSQL 18 (released September 25, 2025; now at 18.6 as of August 13, 2026) ships a real asynchronous I/O subsystem: a new io_method GUC with sync, worker (default, 3 I/O workers), and io_uring modes, effective_io_concurrency raised from 1 to 16, and a pg_aios view showing I/Os in flight. Sequential scans, bitmap heap scans, and vacuum reads get up to 2-3x faster on cold cache; writes and WAL are untouched until 19. The catch: io_uring needs a liburing-enabled build, and Docker's default seccomp profile blocks it."
reading_time: 6
tags: [postgresql, io-uring, async-io, performance, databases]
sources:
  - title: "PostgreSQL 18 Released! — postgresql.org"
    url: "https://www.postgresql.org/about/news/postgresql-18-released-3142/"
  - title: "PostgreSQL 18 documentation: Resource Consumption (io_method, io_workers)"
    url: "https://www.postgresql.org/docs/18/runtime-config-resource.html"
  - title: "Waiting for Postgres 18: Accelerating Disk Reads with Asynchronous I/O — pganalyze"
    url: "https://pganalyze.com/blog/postgres-18-async-io"
  - title: "PostgreSQL 18.6, 17.11, 16.15, 15.19, 14.24, and 19 Beta 3 Released!"
    url: "https://www.postgresql.org/about/news/postgresql-186-1711-1615-1519-1424-and-19-beta-3-released-3365/"
---

For thirty years, when a Postgres backend needed a page that wasn't in shared buffers, it called `pread()` and stopped dead until the kernel produced the bytes. The workarounds were indirect: `posix_fadvise()` hints, OS readahead heuristics, and hope. **PostgreSQL 18** — released September 25, 2025, and now at minor 18.6 (August 13, 2026) — finally lands the asynchronous I/O subsystem Andres Freund spent the better part of a decade building. Backends can now queue batches of reads and keep executing while the kernel fills buffers, and on Linux the mechanism underneath can be **io_uring** — the [two-ring submission/completion design](/articles/linux-tools/2026-07-26-io-uring-async-syscalls) that eliminates a syscall per I/O. On cold-cache scans, reputable benchmarks show 2–3x.

## The three io_methods

Everything hangs off one new GUC, **`io_method`** (restart required):

| `io_method` | Mechanism | Default? | Platform |
|---|---|---|---|
| `sync` | synchronous `pread()` + `posix_fadvise`, ~PG17 behavior | no | all |
| `worker` | pool of dedicated I/O worker processes (**`io_workers`**, default 3) | **yes** | all |
| `io_uring` | one io_uring instance per backend, kernel completes reads in-ring | no | Linux 5.1+, built `--with-liburing` |

`worker` is the conservative default: backends hand read requests to shared I/O workers over shared memory, and it works on every OS. Three workers is deliberately modest — an NVMe-backed box doing heavy sequential work often wants 8–16 (`io_workers` is `postgresql.conf`-settable, no restart in 18.x for the count itself, but watch CPU on the workers). `io_uring` removes the middleman: each backend owns a submission/completion ring pair and the kernel writes completions directly, so there is no per-I/O process hop at all. In pganalyze's cold-cache test of a 3.5GB sequential scan, PG17 took 15.8s, PG18 `worker` 10.1s, and PG18 `io_uring` 5.7s — roughly 2.8x over the old read path.

Two related defaults moved: **`effective_io_concurrency`** jumped from 1 to **16**, and `maintenance_io_concurrency` is also 16. These now mean what they say — the number of I/Os Postgres itself keeps in flight — rather than being a `posix_fadvise` hint multiplier. `io_max_concurrency` (default -1 = auto, capped at 64) bounds per-backend in-flight I/O.

## What's async, what isn't

Read paths converted in 18: **sequential scans**, **bitmap heap scans**, and **vacuum** (including analyze's block sampling). That list is shorter than people assume: ordinary B-tree index scans and index-only scans still read synchronously, and *all writes* — WAL, checkpoints, backend flushes — remain the old code. This is deliberate sequencing, not a limitation of the design; PostgreSQL 19 (beta 3 shipped August 13, 2026) continues extending AIO coverage. So the wins concentrate where cold data meets big scans: analytics queries, `pg_dump`, vacuum on large tables, sequential-heavy batch jobs. A pgbench OLTP workload hitting hot shared buffers will barely notice.

## Turning it on and proving it works

```ini
# postgresql.conf — requires restart
io_method = 'io_uring'          # or 'worker'
io_workers = 8                  # only used by io_method=worker
effective_io_concurrency = 32   # per-backend read window for scans
shared_buffers = '8GB'
```

Verification from psql:

```sql
SHOW io_method;
SHOW server_version;            -- 18.6

-- watch I/Os in flight during a cold sequential scan
SELECT pg_prewarm('big') \gset  -- or just seq scan something cold
SELECT state, operation, count(*)
FROM   pg_aios GROUP BY 1, 2;
```

**`pg_aios`** is the new observability surface: one row per in-flight async I/O handle, with the operation (`readv`), target file, offset, length, and state. If it's empty during a cold multi-gigabyte `SELECT count(*)`, you are not actually doing async I/O — check `io_method` and whether the relation is already cached. `EXPLAIN (ANALYZE, BUFFERS)` shows `shared read` counts as before; the AIO win shows up as wall-clock time, not different plans. The cumulative view `pg_stat_io` (added in 16) also grew `read_bytes`/`write_bytes` columns in 18, so you can attribute I/O volume per backend type over time rather than eyeballing a single scan.

One packaging note: `io_uring` support is compile-time (`--with-liburing` / `-Dliburing=enabled`). The PGDG apt/yum packages ship with it; if `ALTER SYSTEM SET io_method = 'io_uring'` fails at startup with "not supported by this build," your binaries weren't linked against liburing.

## The Linux and container gotchas

`io_method = 'io_uring'` imports io_uring's operational baggage, which is nontrivial in containerized fleets:

- **Seccomp**: Docker's default profile has blocked `io_uring_setup`/`io_uring_enter`/`io_uring_register` since early 2023 (Docker 23.0, after a string of io_uring CVEs; containerd and Kubernetes' `RuntimeDefault` follow suit). Postgres in a container will fail to start with `EPERM` unless you ship a custom seccomp profile that allowlists the three syscalls — don't reach for `seccomp=unconfined`.
- **Sysctl**: kernels ≥ 6.6 have `kernel.io_uring_disabled` (0/1/2). Hardened hosts set `2` (disabled entirely); `1` restricts creation to processes with `CAP_SYS_ADMIN` or a group named by `kernel.io_uring_group`. Check it before debugging anything else.
- **RLIMIT_MEMLOCK**: on kernels older than 5.12 the rings count against locked memory, and Postgres creates one ring *per backend* — hundreds of connections can exhaust a stingy `memlock` ulimit at scale. Modern kernels account rings to memcg instead, so this is mostly a legacy-host concern.

If any of these bite, `io_method = 'worker'` gets you most of the benefit with zero syscall exotica — it's plain `pread()` from worker processes, allowed everywhere. That's the pragmatic fleet default in 2026: `worker` universally, `io_uring` on bare-metal or VM hosts you control end-to-end.

**Try next:** on a Linux box with PGDG 18.6, create a 10GB table, then time `SELECT count(*)` cold (`echo 3 > /proc/sys/vm/drop_caches`, restart Postgres) under `io_method = 'sync'`, `'worker'` with `io_workers = 3` and `8`, and `'io_uring'` — while a second session polls `pg_aios` — and record the four numbers; the spread on your own storage is worth more than any published benchmark.
