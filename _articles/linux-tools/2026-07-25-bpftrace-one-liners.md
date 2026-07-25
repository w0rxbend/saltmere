---
title: "bpftrace one-liners: ask the kernel what it's actually doing"
date: 2026-07-25
track: linux-tools
summary: "strace shows one process; perf needs a workflow. bpftrace lets you attach an eBPF probe to almost any kernel event and aggregate it in the kernel from a one-line program — which file is being opened, which process is burning syscalls, how slow your disk really is."
reading_time: 5
tags: [bpftrace, ebpf, tracing, kernel]
sources:
  - title: "bpftrace — One-Liner Tutorial (official)"
    url: "https://bpftrace.org/tutorial-one-liners"
  - title: "bpftrace releases (latest stable versions)"
    url: "https://github.com/bpftrace/bpftrace/releases"
  - title: "Brendan Gregg — A thorough introduction to bpftrace"
    url: "https://www.brendangregg.com/blog/2019-08-19/bpftrace.html"
  - title: "bpftrace tools/biolatency.bt (block I/O latency)"
    url: "https://github.com/bpftrace/bpftrace/blob/master/tools/biolatency.bt"
---

`strace` follows one process and slows it to a crawl. `perf` is powerful but wants a whole workflow. bpftrace sits in the gap: you write a one-line program, it compiles to eBPF, the kernel runs it in-place on the events you named, and aggregation happens *in the kernel* so you're not shipping every event to userspace. As of mid-2025 the current stable release is **bpftrace 0.26.1**. You need **root** (or `CAP_BPF` + `CAP_PERFMON`) and a reasonably recent kernel built with **BTF** (`/sys/kernel/btf/vmlinux` exists) — with BTF, bpftrace reads kernel struct layouts itself, so you rarely install kernel headers anymore.

## The shape of every program: probe / predicate / action

One grammar covers everything:

```
probe /predicate/ { action }
```

The **probe** names where to attach (`tracepoint:syscalls:sys_enter_openat`). The optional **predicate** is a filter in slashes (`/pid == 1817/`). The **action** runs on each hit. The star of the show is the **map**, any variable starting with `@`: an in-kernel associative array you index by a key and fill with an aggregating function like `count()` or `hist()`. bpftrace prints all maps automatically on exit (Ctrl-C).

## Who is opening this file?

```bash
bpftrace -e 'tracepoint:syscalls:sys_enter_openat {
  printf("%s %s\n", comm, str(args.filename)); }'
```

`comm` is the process name; `args.filename` reaches into the tracepoint's arguments, and `str()` copies the string in from kernel memory. (Note the dot: current bpftrace uses `args.filename`, not the older `args->filename`.) Run it and you'll see every open on the box in real time — invaluable when a config file "isn't being read" and you need proof of which path a process actually touched.

## What's hammering the syscall interface?

```bash
bpftrace -e 'tracepoint:raw_syscalls:sys_enter { @[comm] = count(); }'
```

`@[comm] = count()` keeps a per-process tally entirely in the kernel. Ctrl-C and you get a sorted table of which processes issue the most syscalls — the first question worth asking when a machine is mysteriously busy in `sys` time.

## How big are those reads?

```bash
bpftrace -e 'tracepoint:syscalls:sys_exit_read /comm == "nginx"/ {
  @bytes = hist(args.ret); }'
```

`hist()` builds a power-of-two histogram in the kernel; `args.ret` on the exit tracepoint is the syscall's return value (bytes read). The predicate scopes it to one program. You get an ASCII distribution — instantly obvious whether a service does a few big reads or a storm of tiny ones.

## How slow is the disk, really?

Latency needs two probes and a timestamp stashed in a map between them — the core bpftrace idiom, taken straight from the `biolatency` tool:

```bash
bpftrace -e '
tracepoint:block:block_bio_queue { @start[args.sector] = nsecs; }
tracepoint:block:block_rq_complete /@start[args.sector]/ {
  @usecs = hist((nsecs - @start[args.sector]) / 1000);
  delete(@start, args.sector); }'
```

`nsecs` is a high-resolution clock; you key `@start` by disk sector at queue time, then at completion subtract to get microseconds and feed it to `hist()`. `delete()` frees the slot so the map doesn't grow unbounded. The output is a real block-I/O latency distribution — the same number your monitoring smooths into a misleading average.

## Who's dialing out?

```bash
bpftrace -e 'kprobe:tcp_v4_connect { printf("%-16s -> connect()\n", comm); }'
```

`kprobe:` attaches to an arbitrary kernel function — here the one behind every outbound IPv4 TCP connect. Tracepoints are stable ABI and should be preferred when one exists; kprobes reach anything else, at the cost of breaking if the kernel renames the function. This is how you catch the process quietly phoning home.

**Try next:** turn the disk-latency one-liner into a saved script (`biolatency.bt`), add an `interval:s:5 { print(@usecs); clear(@usecs); }` probe so it prints a fresh histogram every five seconds, and run it while you `dd` a big file. Watching the distribution shift in real time is the moment eBPF stops being abstract.
