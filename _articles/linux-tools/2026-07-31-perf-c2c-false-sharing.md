---
title: "perf c2c: locating the cacheline two threads contend for"
date: 2026-07-31
track: linux-tools
summary: "A flame graph identifies which function burns CPU but cannot see two threads bouncing one 64-byte cacheline between cores. perf c2c samples memory loads and stores, ranks cachelines by remote HITM, and reports the struct offset and source line where false sharing occurs. This article covers the record-to-report workflow and a C program that exhibits the defect and its fix."
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

**Gist.** A multithreaded program can slow down as threads are added while every thread remains busy and no single function appears expensive, because the cost is spread across memory accesses that ought to be nearly free — **cache coherency traffic**, cores taking turns invalidating each other's copy of a shared cacheline. `perf c2c` (cache-to-cache) uses hardware memory-access sampling to rank cachelines by cross-core contention and report the byte offset inside a structure at which the contention occurs. The cost of the diagnosis is a high-rate, system-wide sampling session requiring elevated privileges; the cost of the usual fix is memory, since separating two contended fields onto distinct lines inflates the structure.

## The coherency mechanism behind the symptom

CPUs move memory between caches in fixed units, commonly **64 bytes**, called **cachelines**, and **coherency state is tracked per line, not per byte**. Under the MESI protocol (Modified, Exclusive, Shared, Invalid), a line held in the Modified state by one core is the sole valid copy. When a core writes any byte within a line, every other core holding that line must drop its copy to Invalid. A subsequent read on another core cannot be served locally: the data must come from the writer's cache, or from memory once the modified line has been written back. That event is a **HITM** — a load that *hits* a line in the *Modified* state in another core's cache. A HITM sourced from a cache on a different socket is a **remote** HITM and is the more expensive of the two.

The invariant is per line: **two independent variables placed in one line are indistinguishable from one variable, as far as the coherency protocol is concerned.**

**False sharing** is the case where two threads write two *logically independent* variables that happen to occupy the same line:

```c
struct counters {
    uint64_t a;   // thread 0 increments this
    uint64_t b;   // thread 1 increments this
};                // sizeof == 16, so a and b share ONE cacheline
```

Thread 0 never touches `b` and thread 1 never touches `a`, so the program is correct. Every `a++` nevertheless invalidates thread 1's copy of the line and every `b++` invalidates thread 0's. The line ping-pongs between cores, and each iteration pays cache-miss latency rather than the L1 hit latency the source suggests. **True sharing** — both threads writing the same variable, for example a shared atomic counter — produces the same HITM signature. `perf c2c` reports both; **the offset column is what distinguishes them**, since true sharing concentrates all HITM on a single offset while false sharing spreads it across two or more.

## How the sampling works

`perf c2c record` does not instrument the program. On Intel it arms two Precise Event-Based Sampling (PEBS) events, `cpu/mem-loads,ldlat=30/P` and `cpu/mem-stores/P`. For a sampled memory access these capture the **data address**, the **access type**, and the **load latency in cycles**. The `ldlat=30` threshold makes **only loads that took at least 30 cycles eligible**, which restricts the sampled population to accesses slow enough to have been served from somewhere other than a nearby cache level. AMD uses Instruction-Based Sampling (IBS) and Arm uses the Statistical Profiling Extension (SPE); the principle is the same, in that hardware tags a subset of loads and stores with the location the data was sourced from.

Because the events are hardware performance-monitoring events, recording requires `CAP_PERFMON` (or root) and a `perf_event_paranoid` setting permissive enough to allow them.

```console
# high sample rate, all CPUs, user-space accesses only, for a running binary
$ sudo perf c2c record -F 60000 -a --all-user -- ./false_sharing
$ sudo perf c2c report -NN -c pid,iaddr --stdio
```

`-c pid,iaddr` groups the Pareto output by process and instruction address, so the report identifies the exact program counter performing the access; `-NN` prints full symbol names; `--stdio` emits text rather than the interactive terminal interface.

## Reading the report

Two tables carry the diagnosis.

The **Trace Event Information** header reports the share of last-level-cache misses satisfied from a remote cache in the line `LLC Misses to Remote Cache (HITM)`. **A non-trivial percentage there is the signal that cross-socket cacheline contention is present and the rest of the report is worth reading.** A near-zero percentage does not by itself rule false sharing out, because contention confined to one socket is counted as local rather than remote.

The **Shared Data Cache Line Table** lists one 64-byte line per row, sorted so that the most-contended line is row 0, with its virtual address and its `Rmt LLC Load Hitm` and `Lcl LLC Load Hitm` counts. Expanding the hottest line into the **Pareto** distribution yields the offset-level breakdown:

```
=================================================
      Shared Cache Line Distribution Pareto
=================================================
--- cacheline 0x557... ---
  Rmt  Lcl   Data offset   Pid    code addr   symbol        object   cpu
  51%  49%   0x0           4123   worker_a    ./false_sharing  0,1..
  49%  51%   0x8           4124   worker_b    ./false_sharing  0,1..
```

**Two distinct offsets (`0x0` and `0x8`) within the same line, written by two different threads running on different CPUs, is the false-sharing fingerprint.** Field `a` occupies offset 0 and field `b` offset 8; both are inside one line and both are hot. Were a single offset to account for all the HITM traffic, the diagnosis would instead be true sharing of one variable, and padding would not help.

## Separating the writers onto distinct lines

The fix is to pad or align the structure so that the two fields cannot coexist in a line. C11's `alignas`, declared in `<stdalign.h>`, places each field on its own 64-byte boundary:

```c
#include <stdalign.h>
#include <stdint.h>

struct counters {
    alignas(64) uint64_t a;   // own cacheline
    alignas(64) uint64_t b;   // own cacheline
};                            // sizeof == 128 now
```

In C++17, `alignas(std::hardware_destructive_interference_size)` names the intent rather than hard-coding a line size. **The structure doubles in size: memory is exchanged for cache independence.** Re-running `perf c2c record` after the change shows the remote-HITM percentage for that line collapsing toward zero.

For per-thread scratch state, an alternative that avoids the padding entirely is to not share the structure: give each thread its own object, or use per-CPU data (`__thread` in user space, `alloc_percpu` in kernel code), so that the line is never held by more than one core.

A reproduction that exercises the whole workflow: compile the two-field `struct counters` above with two threads each performing a large number of increments, time the run, then record with `sudo perf c2c record -a --all-user -- ./yourbin` and confirm that both offsets appear under a single cacheline in `perf c2c report --stdio`. Adding `alignas(64)` to both fields, rebuilding, and comparing wall-clock time against the HITM count isolates the effect of the alignment change alone.

## Pitfalls

- **A zero HITM percentage on a single-socket machine does not prove the absence of contention.** The remote-HITM counter concerns lines sourced from another socket's cache; contention confined to one socket appears under `Lcl LLC Load Hitm` instead.
- **Recording without `--all-user` mixes kernel accesses into the report,** and the hottest lines can then be kernel data structures rather than anything in the profiled program.
- **The offset column is meaningless without the symbol.** A hot line at two offsets in an anonymous mapping identifies contention but not which fields are involved; the binary must retain symbols, and `-NN` must be passed for the full names to be printed.
- **`ldlat=30` filters out contention that resolves quickly.** Loads completing in fewer than 30 cycles are never sampled, so a workload whose sharing is cheap on a given machine can register as clean.
- **Padding a structure whose HITM concentrates on one offset changes nothing.** That signature is true sharing, where the threads genuinely write the same variable, and the remedy is algorithmic — sharding the counter or removing the shared write — not alignment.
- **`perf c2c record` needs `CAP_PERFMON` or root and a permissive `perf_event_paranoid`;** without them the memory events fail to arm, and the run produces an empty or truncated report rather than an explicit diagnosis of the sharing.
- **Alignment applies to the type, not to every allocation path.** A structure whose fields are declared `alignas(64)` still requires an allocator that honours over-aligned types, or the fields can land at offsets other than the intended boundaries.
