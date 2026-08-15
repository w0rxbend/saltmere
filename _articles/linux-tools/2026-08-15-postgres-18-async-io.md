---
title: "Postgres 18 Asynchronous I/O: io_uring in the Read Path"
date: 2026-08-15
track: linux-tools
summary: "PostgreSQL 18 (released September 25, 2025; 18.6 as of August 13, 2026) ships an asynchronous I/O subsystem: an io_method setting with sync, worker (default, 3 I/O workers) and io_uring modes, effective_io_concurrency raised from 1 to 16, and a pg_aios view exposing I/Os in flight. Sequential scans, bitmap heap scans and vacuum reads benefit; writes and the write-ahead log are unchanged. io_uring requires a liburing-enabled build, and Docker's default seccomp profile blocks the required syscalls."
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

**Gist.** Until PostgreSQL 18, a backend that needed a page absent from shared buffers issued a synchronous `pread()` and blocked until the kernel returned the bytes, leaving storage parallelism to `posix_fadvise()` hints and operating-system readahead. **PostgreSQL 18 adds an asynchronous I/O (AIO) subsystem**: a backend submits a batch of reads, continues executing, and collects completions later, with the submission mechanism selected by the **`io_method`** setting — including **io_uring**, the [two-ring submission/completion interface](/articles/linux-tools/2026-07-26-io-uring-async-syscalls) that removes a system call per I/O. The cost is a narrower deployment envelope: io_uring is a compile-time option, needs Linux 5.1 or later, and is blocked by the default container seccomp (secure computing mode) profile, so the portable mode remains a process pool that still performs ordinary blocking reads.

PostgreSQL 18 was released September 25, 2025; the current minor release is 18.6, dated August 13, 2026. The subsystem is the result of work by Andres Freund carried over many release cycles.

## The three io_methods

All behaviour hangs off one setting, **`io_method`**, which requires a server restart to change.

| `io_method` | Mechanism | Default | Platform |
|---|---|---|---|
| `sync` | synchronous `pread()` plus `posix_fadvise`, equivalent to PostgreSQL 17 behaviour | no | all |
| `worker` | pool of dedicated I/O worker processes (**`io_workers`**, default 3) | **yes** | all |
| `io_uring` | one io_uring instance per backend; the kernel completes reads in-ring | no | Linux 5.1+, built `--with-liburing` |

Under `worker`, a backend places a read request in shared memory and an I/O worker process performs the read on its behalf. The submitting backend is free to continue, but **the read itself is still a blocking `pread()`, merely executed in another process**, so the mechanism costs a process hop and shared-memory handoff per I/O and works on every supported operating system.

Under `io_uring`, **each backend owns its own submission and completion ring pair**; requests are written into the submission queue and the kernel posts completions directly into the completion queue, so no separate PostgreSQL process participates. The per-I/O process hop disappears.

The relevant concurrency knobs moved with the subsystem. **`effective_io_concurrency` changed from 1 to 16**, and `maintenance_io_concurrency` is also 16. Both now denote the number of I/Os PostgreSQL itself keeps in flight, rather than acting as a multiplier on `posix_fadvise` hints. **`io_max_concurrency`** (default `-1`, meaning a value derived automatically from `shared_buffers` and `max_connections`) bounds in-flight I/O per backend.

The pganalyze write-up reports a cold-cache sequential scan completing faster under both new methods than under the synchronous path, with `io_uring` ahead of `worker`. That is a single workload on a single storage device; it bounds nothing about other hardware, and no published benchmark separates the three methods across a range of storage types.

## Converted and unconverted paths

The read paths converted in 18 are **sequential scans**, **bitmap heap scans**, and **vacuum**, including the block sampling performed by `ANALYZE`. Paths that remain synchronous include **ordinary B-tree index scans and index-only scans**, and **all write paths**: the write-ahead log (WAL), checkpoints, and backend buffer flushes. PostgreSQL 19 is in beta as of August 13, 2026; which further paths it converts is not settled by the 18 documentation.

The consequence for workload selection follows directly from that list. Gains concentrate where **cold data meets large scans**: analytics queries, `pg_dump`, vacuum over large tables, sequential-heavy batch jobs. An OLTP (online transaction processing) workload whose working set is resident in shared buffers issues few physical reads and therefore has little for the subsystem to overlap.

## Configuration and verification

```ini
# postgresql.conf — requires restart
io_method = 'io_uring'          # or 'worker'
io_workers = 8                  # used only by io_method=worker
effective_io_concurrency = 32   # per-backend read window for scans
shared_buffers = '8GB'
```

Verification from `psql`:

```sql
SHOW io_method;
SHOW server_version;            -- 18.6

-- observe I/Os in flight during a cold sequential scan
SELECT state, operation, count(*)
FROM   pg_aios GROUP BY 1, 2;
```

**`pg_aios`** is the new observability surface: **one row per in-flight asynchronous I/O handle**, carrying the operation (`readv`), the target file, the offset, the length, and the handle state. Because the rows describe only I/Os currently outstanding, the view is empty whenever nothing is in flight — an empty result during a cold multi-gigabyte `SELECT count(*)` indicates either that `io_method` is `sync` or that the relation is already cached, not that the query is inexpensive.

`EXPLAIN (ANALYZE, BUFFERS)` reports `shared read` counts exactly as before, and plan shapes are unchanged; **the effect of AIO appears as wall-clock time, not as a different plan**. The cumulative view `pg_stat_io`, introduced in PostgreSQL 16, gained `read_bytes` and `write_bytes` columns in 18, which allows I/O volume to be attributed per backend type over time rather than inferred from a single scan.

io_uring support is a build-time option (`--with-liburing`, or `-Dliburing=enabled` under Meson). The PGDG apt and yum packages are built with it. A binary built without it rejects the setting: `ALTER SYSTEM SET io_method = 'io_uring'` followed by a restart fails with a message stating the method is not supported by this build.

## Linux and container constraints

Selecting `io_method = 'io_uring'` imports io_uring's operational constraints into the database process.

- **Seccomp.** Docker's default seccomp profile has blocked `io_uring_setup`, `io_uring_enter` and `io_uring_register` since Docker 23.0; containerd and the Kubernetes `RuntimeDefault` profile follow the same policy. A containerised PostgreSQL configured for io_uring fails at startup with `EPERM` unless a custom profile allowlists those three system calls. Allowlisting three calls is narrower than `seccomp=unconfined`, which removes the filter entirely.
- **Sysctl.** Kernels 6.6 and later expose `kernel.io_uring_disabled` with values 0, 1 and 2. A value of **2 disables io_uring entirely**; **1 restricts ring creation to processes holding `CAP_SYS_ADMIN` or belonging to the group named by `kernel.io_uring_group`**. Hardened host images commonly set 2.
- **RLIMIT_MEMLOCK.** On kernels older than 5.12, ring memory counts against the locked-memory limit, and **PostgreSQL creates one ring per backend**, so a connection count in the hundreds can exhaust a small `memlock` limit. Kernels from 5.12 account ring memory to the control group instead, which confines this to legacy hosts.

Where any of these constraints apply, `io_method = 'worker'` remains available: it performs ordinary `pread()` calls from worker processes and requires no io_uring syscalls, at the cost of the per-I/O process hop that separates it from `io_uring` in the pganalyze measurement.

## Pitfalls

- **Empty `pg_aios` read as "no I/O".** The view lists only outstanding handles; a cached relation or `io_method = 'sync'` produces zero rows during a scan that nonetheless reads every page.
- **Expecting index scans to accelerate.** B-tree index scans and index-only scans were not converted in 18, so a plan dominated by index access shows no wall-clock change after switching `io_method`.
- **Expecting write or WAL latency to change.** All write paths remain synchronous in 18; checkpoint and WAL flush behaviour is unaffected by `io_method`.
- **Container startup failing with `EPERM`.** The default Docker and `RuntimeDefault` seccomp profiles block the three io_uring system calls, so the failure occurs at server start rather than at query time.
- **Silent unavailability on hardened hosts.** `kernel.io_uring_disabled = 2` prevents ring creation regardless of capabilities or seccomp configuration, and `= 1` restricts it to `CAP_SYS_ADMIN` or the `kernel.io_uring_group` group.
- **Setting `io_method` without a restart.** The parameter is restart-only; a reload leaves the previous method in force while `postgresql.conf` shows the new value.
- **Raising `io_workers` without watching worker CPU.** Under `worker`, every request costs a process hop, so added workers convert I/O wait into process-level CPU consumption.
- **Assuming a build supports io_uring.** Support is compile-time; a binary lacking liburing rejects the setting at startup rather than falling back to another method.
