---
title: "perf c2c: find the cacheline two threads are fighting over"
date: 2026-07-31
track: linux-tools
summary: "A flame graph shows you which function burns CPU, but it can't see two threads bouncing one 64-byte cacheline between cores. perf c2c samples memory loads and stores, ranks cachelines by remote HITM, and points at the exact struct offset and source line where false sharing lives. Here's the record-to-report workflow and a C program that exhibits the bug and its fix."
reading_time: 6
tags: [perf, c2c, false-sharing, cache-coherency, numa, linux-tools, performance]
sources:
  - title: "perf-c2c(1) — Linux manual page"
    url: "https://man7.org/linux/man-pages/man1/perf-c2c.1.html"
  - title: "C2C - False Sharing Detection in Linux Perf (Joe Mario, 2016)"
    url: "https://joemario.github.io/blog/2016/09/01/c2c-blog/"
  - title: "Red Hat Enterprise Linux 9: Detecting false sharing"
    url: "https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html/monitoring_and_managing_system_status_and_performance/detecting-false-sharing_monitoring-and-managing-system-status-and-performance"
  - title: "Detecting False Sharing with Perf C2C (CoffeeBeforeArch)"
    url: "https://coffeebeforearch.github.io/2020/03/27/perf-c2c.html"
---

A CPU flame graph answers "which function is hot." It is blind to a different, nastier class of scaling bug: your code adds threads and gets *slower*, every thread is busy, and no single function looks expensive because the cost is spread across a memory access that should have been free. That cost is **cache coherency traffic** — cores taking turns invalidating each other's copy of a cacheline. `perf c2c` (cache-to-cache) is the tool built specifically to find it, and it reports down to the byte offset inside a struct.

## What false sharing actually is

CPUs move memory in 64-byte units called **cachelines**, and coherency is tracked per line, not per byte. When a core writes any byte in a line, the MESI protocol forces every other core holding that line to drop its copy. The next core to read must re-fetch the modified data from the writer's cache — a **HITM** (a load that *hits* a line in the *Modified* state in another core's cache).

**False sharing** is when two threads write two *logically independent* variables that happen to land in the same line:

```c
struct counters {
    uint64_t a;   // thread 0 increments this
    uint64_t b;   // thread 1 increments this
};                // sizeof == 16, so a and b share ONE cacheline
```

Thread 0 never touches `b` and thread 1 never touches `a`, so the program is correct — but every `a++` invalidates thread 1's line and every `b++` invalidates thread 0's. The line ping-pongs between cores at cache-miss latency on every iteration. **True sharing** (both threads hammering the same variable, e.g. a shared atomic) produces the same HITM signature; c2c finds both, and the offset column tells them apart.

## How the sampling works

`perf c2c record` doesn't instrument anything. On Intel it arms two PEBS (Precise Event-Based Sampling) events — `cpu/mem-loads,ldlat=30/P` and `cpu/mem-stores/P` — that capture, for a sampled memory access, the **data address**, the **access type**, and the **load latency in cycles**. The `ldlat=30` threshold means only loads that took at least 30 cycles are eligible, which is exactly the population you care about: a line served from a remote core's cache is slow, an L1 hit isn't. AMD uses IBS and Arm uses SPE, but the idea is identical — hardware tags a subset of loads/stores with where the data came from.

Record system-wide while the workload runs (needs `CAP_PERFMON` or root, and `perf_event_paranoid` low enough):

```console
# high sample rate, all CPUs, user-space accesses only, for a running binary
$ sudo perf c2c record -F 60000 -a --all-user -- ./false_sharing
$ sudo perf c2c report -NN -c pid,iaddr --stdio
```

`-c pid,iaddr` groups the Pareto output by process and instruction address so you see the exact PC doing the damage; `-NN` shows full symbol names; `--stdio` dumps text instead of the TUI.

## Reading the report

Two tables matter. First, the **Trace Event Information** header gives you the go/no-go signal — the line `LLC Misses to Remote Cache (HITM)`. If that percentage is more than a few percent, you have cross-core cacheline contention worth chasing. Zero means false sharing is not your problem.

Second, the **Shared Data Cache Line Table**, sorted so the most-contended line is row 0. Each row is one 64-byte line with its virtual address and its `Rmt LLC Load Hitm` / `Lcl LLC Load Hitm` counts. Expand the hottest line into the **Pareto** distribution and you get the payoff:

```
=================================================
      Shared Cache Line Distribution Pareto
=================================================
--- cacheline 0x557... ---
  Rmt  Lcl   Data offset   Pid    code addr   symbol        object   cpu
  50%  49%   0x0           4123   worker_a    ./false_sharing  0,1..
  49%  50%   0x8           4124   worker_b    ./false_sharing  0,1..
```

Two different **offsets** (`0x0` and `0x8`) in the *same* line, written from two different threads on different CPUs — that is the textbook false-sharing fingerprint. `a` sits at offset 0, `b` at offset 8, both inside one line, both hot. (If a *single* offset showed all the HITM, that would be true sharing of one variable instead.)

## The fix: give each writer its own line

Pad or align so the two fields can never coexist in a line. C11's `alignas` (via `<stdalign.h>`) forces each field onto its own 64-byte boundary:

```c
#include <stdalign.h>
#include <stdint.h>

struct counters {
    alignas(64) uint64_t a;   // own cacheline
    alignas(64) uint64_t b;   // own cacheline
};                            // sizeof == 128 now
```

In C++17, prefer `alignas(std::hardware_destructive_interference_size)`, which names the intent. The struct doubles in size — that is the deliberate trade: you spend memory to buy back cache independence. Re-run `perf c2c record` and the remote-HITM percentage on that line collapses toward zero. For per-thread scratch state the cleaner answer is often to not share the struct at all — give each thread its own object, or use **per-CPU data** (`__thread`, or `alloc_percpu` in kernel code) so the line is never contended in the first place.

**Try next:** Compile the 2-field `struct counters` above with two threads each spinning a billion increments, time it, then run `sudo perf c2c record -a --all-user -- ./yourbin` and confirm both offsets show up under one cacheline in `perf c2c report --stdio`. Add `alignas(64)` to both fields, rebuild, and compare the wall-clock time and the HITM count — on most multi-socket boxes you'll see a multiple-x speedup from a one-line change.
