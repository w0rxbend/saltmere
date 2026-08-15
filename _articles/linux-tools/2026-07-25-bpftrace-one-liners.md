---
title: "bpftrace one-liners: in-kernel aggregation of kernel events"
date: 2026-07-25
track: linux-tools
summary: "strace follows one process; perf wants a workflow. bpftrace attaches an extended Berkeley Packet Filter (eBPF) probe to almost any kernel event and aggregates it inside the kernel from a one-line program — which file is opened, which process burns syscalls, how block I/O latency is actually distributed."
reading_time: 6
tags: [bpftrace, ebpf, tracing, kernel]
sources:
  - title: "bpftrace — One-Liner Tutorial (official)"
    url: "https://bpftrace.org/tutorial-one-liners"
  - title: "bpftrace releases"
    url: "https://github.com/bpftrace/bpftrace/releases"
  - title: "Brendan Gregg — A thorough introduction to bpftrace"
    url: "https://www.brendangregg.com/blog/2019-08-19/bpftrace.html"
  - title: "bpftrace tools/biolatency.bt (block I/O latency)"
    url: "https://github.com/bpftrace/bpftrace/blob/master/tools/biolatency.bt"
---

**Gist.** Observing kernel behaviour with `strace` costs a trap per syscall and covers one process; `perf` records events and analyses them afterwards. bpftrace compiles a short program to extended Berkeley Packet Filter (eBPF) bytecode, the kernel attaches it to named events, and **the aggregation happens in kernel space**, so only the summary crosses into userspace instead of one record per event. The cost is a privilege requirement — root, or the `CAP_BPF` and `CAP_PERFMON` capabilities — plus a probe surface that is partly unstable: kprobes attach to internal kernel functions that can be renamed or inlined between releases.

A kernel built with BPF Type Format (BTF) exposes `/sys/kernel/btf/vmlinux`; with BTF present bpftrace reads kernel struct layouts from the running kernel itself, so kernel headers are usually not required.

## The grammar: probe, predicate, action

Every program has the same shape:

```
probe /predicate/ { action }
```

The **probe** names the attachment point (`tracepoint:syscalls:sys_enter_openat`). The **predicate**, optional, is a filter in slashes (`/pid == 1817/`) evaluated per event; a false predicate skips the action. The **action** runs on each hit.

The load-bearing construct is the **map**: any variable whose name begins with `@` is an in-kernel associative array, indexed by an optional key and updated by an aggregating function such as `count()` or `hist()`. **Because the map lives in kernel memory and is mutated in place, per-event cost is a hash lookup and an increment rather than a userspace round trip.** bpftrace prints every map automatically when the program exits, which is why one-liners need no explicit print statement.

## Which process opens which path

```bash
bpftrace -e 'tracepoint:syscalls:sys_enter_openat {
  printf("%s %s\n", comm, str(args.filename)); }'
```

`comm` is the process name of the task that triggered the probe. `args.filename` reads the tracepoint's argument structure, and `str()` copies the string out of kernel memory into the program's buffer — **the raw argument is a pointer, so dereferencing it without `str()` prints an address.** Current bpftrace spells field access with a dot (`args.filename`); older syntax used `args->filename`.

This probe emits one line per event and so does not benefit from in-kernel aggregation; its use is establishing which path a process opened, rather than which path a configuration file claims.

## Ranking syscall issuers

```bash
bpftrace -e 'tracepoint:raw_syscalls:sys_enter { @[comm] = count(); }'
```

`raw_syscalls:sys_enter` fires on entry to every syscall regardless of number, so it fires far more often than any single-syscall tracepoint. `@[comm] = count()` keys the map by process name and increments in kernel space; the userspace process receives one table at exit rather than one record per syscall. This answers the first question worth asking when a machine spends unexplained time in `sys`.

Adding the syscall number as a second key narrows the answer to which call is responsible:

```bash
bpftrace -e 'tracepoint:raw_syscalls:sys_enter {
  @[comm, args.id] = count(); }'
```

## Distribution of read sizes

```bash
bpftrace -e 'tracepoint:syscalls:sys_exit_read /comm == "nginx"/ {
  @bytes = hist(args.ret); }'
```

`hist()` builds a **power-of-two histogram**: each value falls into a bucket spanning one binary order of magnitude, so the map holds a bounded number of slots regardless of how many events arrive. `args.ret` on the exit tracepoint is the syscall return value, which for `read` is the byte count. The predicate restricts accounting to one program.

Note the sign: a failed `read` returns a negative errno, so **failures are counted in the same histogram as successful reads** unless the predicate excludes them with `args.ret >= 0`.

## Block I/O latency

Latency requires two probes and a timestamp carried in a map between them — the idiom used by the `biolatency` tool:

```bash
bpftrace -e '
tracepoint:block:block_bio_queue { @start[args.sector] = nsecs; }
tracepoint:block:block_rq_complete /@start[args.sector]/ {
  @usecs = hist((nsecs - @start[args.sector]) / 1000);
  delete(@start, args.sector); }'
```

`nsecs` is a high-resolution clock read. The first probe stores the queue time keyed by disk sector; the second subtracts it, converts nanoseconds to microseconds, and feeds the result to `hist()`.

Two details are load-bearing. The predicate `/@start[args.sector]/` **discards completions whose queue event was not observed** — otherwise the subtraction runs against a zero-valued entry and yields a latency roughly equal to system uptime. And `delete()` **releases the map slot**, without which `@start` accumulates one entry per in-flight request that never completes under observation, and the map grows without bound.

The output is a latency distribution rather than the mean that most monitoring pipelines report.

## Attaching where no tracepoint exists

```bash
bpftrace -e 'kprobe:tcp_v4_connect { printf("%-16s -> connect()\n", comm); }'
```

`kprobe:` attaches to an arbitrary kernel function — here the one underlying outbound IPv4 TCP connection setup. Tracepoints are a stable application binary interface (ABI) and are preferable where one exists; kprobes reach everything else at the cost of **breaking when the kernel renames or inlines the target function**, which surfaces as an attach-time error rather than as silently wrong data.

## Periodic output

A saved script turns a one-liner into a monitor. An `interval` probe prints and clears a map on a fixed period:

```bash
#!/usr/bin/env bpftrace
tracepoint:block:block_bio_queue { @start[args.sector] = nsecs; }
tracepoint:block:block_rq_complete /@start[args.sector]/ {
  @usecs = hist((nsecs - @start[args.sector]) / 1000);
  delete(@start, args.sector); }
interval:s:5 { print(@usecs); clear(@usecs); }
```

`print()` emits the map's current contents; `clear()` empties it so the next interval reports a fresh window rather than a cumulative one. **Omitting `clear()` produces a monotonically accumulating histogram**, in which a latency change late in the run is diluted by everything measured before it.

## Pitfalls

- **`args->filename` fails to parse on current bpftrace.** Field access on tracepoint arguments uses a dot; the arrow form belongs to older releases.
- **Printing `args.filename` without `str()` yields a pointer value**, because the tracepoint argument is an address in kernel memory rather than an inline string.
- **A latency map without `delete()` grows without bound.** Every request whose completion is missed leaves a permanent entry keyed by sector.
- **A completion probe without the `/@start[key]/` predicate reports absurd latencies.** A missing start entry reads as zero, so the subtraction returns the current value of `nsecs`.
- **Negative values in a `hist()` of `args.ret` are error returns**, not small transfers; without a predicate on the sign they are counted alongside successful transfers.
- **Sector keys are not unique across devices.** Two block devices can queue the same sector number concurrently, and the second write to `@start[sector]` overwrites the first.
- **A kprobe on a renamed or inlined function fails to attach.** The failure is at program load, so a script that worked on one kernel stops running entirely on another rather than degrading.
- **`raw_syscalls:sys_enter` fires on every syscall on the machine**, so an action that does per-event `printf` rather than in-kernel aggregation floods the output and loses events.
- **Maps print only on exit unless `print()` is called.** A program that is killed with `SIGKILL` rather than interrupted with Ctrl-C produces no output at all.
