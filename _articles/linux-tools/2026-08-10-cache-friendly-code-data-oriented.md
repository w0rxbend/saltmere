---
title: "Cache-friendly code: the memory hierarchy you actually control"
date: 2026-08-10
track: linux-tools
summary: "Your CPU is a race car bolted to a delivery van: it can retire several instructions per cycle but stalls for a hundred-plus cycles waiting on RAM. The caches between them are the one part of the memory hierarchy your data layout directly controls. This walks the L1/L2/L3/RAM latency ladder, cache lines and locality, AoS vs SoA, why linked lists lose to arrays, false sharing, and prefetching — with a row-major vs column-major benchmark you can measure with perf."
reading_time: 6
tags: [cpu-cache, performance, data-oriented-design, perf, cache-lines, false-sharing, linux-tools]
sources:
  - title: "What Every Programmer Should Know About Memory (Ulrich Drepper, 2007)"
    url: "https://people.freebsd.org/~lstewart/articles/cpumemory.pdf"
  - title: "Intel Skylake — measured cache latencies (7-cpu.com)"
    url: "https://www.7-cpu.com/cpu/Skylake.html"
  - title: "Latency Numbers Every Programmer Should Know (Jeff Dean / jboner gist)"
    url: "https://gist.github.com/jboner/2841832"
  - title: "Optimizing software in C++ (Agner Fog)"
    url: "https://www.agner.org/optimize/optimizing_cpp.pdf"
---

Modern CPUs are wildly imbalanced machines. A single core can retire several instructions per cycle, but a load that misses every cache and goes to DRAM costs on the order of a hundred-plus cycles — hundreds of instructions' worth of stall for one pointer dereference. The gap between compute and memory is the dominant performance story on real hardware, and the caches are the one layer of the memory hierarchy your *code* shapes directly. You don't allocate cache; you earn it, by laying out data so the hardware's automatic policies work in your favor.

## The latency ladder

The numbers below are ballparks — they vary by microarchitecture, frequency, and working-set size — but the *ratios* are what matter and they're stable. Measured on an Intel Skylake i7-6700 at ~4 GHz ([7-cpu.com](https://www.7-cpu.com/cpu/Skylake.html)):

| Level | Rough latency | Ballpark |
|-------|---------------|----------|
| L1 data cache | ~4 cycles | ~1 ns |
| L2 cache | ~12 cycles | ~3 ns |
| L3 (LLC) | ~42 cycles | ~10 ns |
| Main memory (DRAM) | ~42 cycles + ~51 ns | ~60–100 ns / 150–250+ cycles |

The classic "[Latency Numbers Every Programmer Should Know](https://gist.github.com/jboner/2841832)" list rounds these to L1 ≈ 0.5 ns, L2 ≈ 7 ns, main memory ≈ 100 ns — same shape, different silicon. The takeaway is one order of magnitude per step down the ladder, and roughly **two orders of magnitude** between L1 and DRAM. A cache miss isn't a small tax; it's the difference between "free" and "the core sits idle for the time it takes to execute a hundred adds."

## Cache lines and locality

The cache doesn't move bytes; it moves **cache lines**. On x86-64 a line is 64 bytes (`getconf LEVEL1_DCACHE_LINESIZE` confirms it). Touch one byte and the hardware hauls in the whole aligned 64-byte block. Two consequences fall out of this, and they're the entire game:

- **Spatial locality**: if you use data near data you just used, it's already in the line — free. Walking an array sequentially gets ~8 `int`s or 8 pointers per miss.
- **Temporal locality**: if you reuse data soon, it's still resident. Loops that revisit a small working set stay hot in L1.

Ulrich Drepper's [*What Every Programmer Should Know About Memory*](https://people.freebsd.org/~lstewart/articles/cpumemory.pdf) is the canonical deep dive here, and its central lesson is exactly this: performance is governed by how your access pattern maps onto lines, sets, and pages — not by instruction count.

## Sequential vs random: why linked lists lose

This is why a `std::vector` beats a `std::list` for almost any traversal, even though both are "O(n)." An array is contiguous: each 64-byte line the prefetcher pulls in serves the next several elements, and the hardware prefetcher spots the linear stride and fetches ahead. A linked list scatters nodes across the heap; every `node = node->next` is a dependent load to an unpredictable address — a likely cache miss that the prefetcher can't hide because it can't know the next address until the current load *completes*. You pay ~100 cycles per hop and serialize on them. Big-O counts operations; it doesn't count that some operations are 100x more expensive than others.

## AoS vs SoA and data-oriented design

Say you have a million particles and a hot loop that only touches position:

```c
// Array of Structs (AoS): fields interleaved in memory
struct Particle { float x, y, z; float vx, vy, vz; int id; char tag[16]; };
struct Particle parts[N];               // ~44+ bytes each

for (int i = 0; i < N; i++)
    parts[i].x += parts[i].vx * dt;     // pulls whole struct into cache
```

Each iteration touches `x` and `vx` but the line drags in `z`, `id`, `tag[]` — bytes you won't use, evicting bytes you might. You're paying DRAM bandwidth for padding.

**Struct of Arrays (SoA)** splits fields into parallel arrays:

```c
struct Particles { float x[N], y[N], z[N], vx[N], vy[N], vz[N]; };
struct Particles p;

for (int i = 0; i < N; i++)
    p.x[i] += p.vx[i] * dt;             // x[] and vx[] are each dense & contiguous
```

Now every byte pulled into a line is a byte you use, and the streams auto-vectorize cleanly (SIMD wants contiguous same-type data). This is the core of **data-oriented design**: design around how data is accessed in bulk, not around "objects." Agner Fog's [*Optimizing software in C++*](https://www.agner.org/optimize/optimizing_cpp.pdf) covers the layout and vectorization mechanics in detail. AoS still wins when you touch *all* fields of *one* object at a time — measure your access pattern, don't cargo-cult SoA.

## The benchmark: row-major vs column-major

The cleanest demonstration is traversing a matrix in memory order versus against it. C arrays are row-major, so `m[i][j]` and `m[i][j+1]` are adjacent; `m[i][j]` and `m[i+1][j]` are `N` floats apart.

```c
#include <stdlib.h>
#define N 8192
static float m[N][N];   // 256 MB — far bigger than any cache

int main(void) {
    double sum = 0;
#ifdef COLMAJOR
    for (int j = 0; j < N; j++)
        for (int i = 0; i < N; i++)   // stride N*4 bytes: new line every element
            sum += m[i][j];
#else
    for (int i = 0; i < N; i++)
        for (int j = 0; j < N; j++)   // stride 4 bytes: ~16 hits per line
            sum += m[i][j];
#endif
    return (int)sum;
}
```

Same arithmetic, same instruction count. The row-major version reuses each 64-byte line ~16 times before moving on; the column-major version touches one float per line and evicts it before ever coming back — nearly a miss per access once the matrix dwarfs the cache. Expect the column-major loop to run roughly **5–10x slower** on typical desktop hardware (larger the deeper you blow past L3).

## Measuring it with perf

Numbers you can defend come from `perf`, not intuition:

```sh
gcc -O2 -o rowmajor  bench.c
gcc -O2 -DCOLMAJOR -o colmajor bench.c

perf stat -e cache-references,cache-misses,L1-dcache-loads,L1-dcache-load-misses,LLC-load-misses \
    ./rowmajor
perf stat -e cache-references,cache-misses,L1-dcache-loads,L1-dcache-load-misses,LLC-load-misses \
    ./colmajor
```

The row-major run shows an L1 miss rate in the low single-digit percent; the column-major run shows `LLC-load-misses` exploding and a miss rate approaching one-per-access, with wall time tracking it. `cache-misses / cache-references` is the headline ratio to quote in an interview.

## Alignment, padding, and false sharing

Two adjacent tuning knobs live at the line boundary. First, **alignment**: a hot struct that straddles two lines costs two fetches; aligning it to 64 bytes (`alignas(64)` / `__attribute__((aligned(64)))`) keeps it in one. Second, and nastier in threaded code, **false sharing**: two threads writing *different* variables that happen to share one 64-byte line force the cache-coherence protocol to bounce that line between cores on every write, even though there's no logical conflict. Throughput collapses as you add threads. The fix is the same knob — pad or align the per-thread data so each hot variable owns its own line. It's easy to introduce by accident and invisible in a flame graph; there's a dedicated, perf-based hunt for it in [perf c2c: find the cacheline two threads are fighting over](/articles/linux-tools/2026-07-31-perf-c2c-false-sharing).

## Prefetching

The hardware prefetcher watches your access stream and, on a detected linear stride, fetches ahead so the line is resident before you ask — which is *why* sequential wins and random loses: predictable strides let the prefetcher hide latency it can't hide for a linked list's random hops. When your pattern is regular but not obviously so, you can hint it with `__builtin_prefetch(ptr, 0, 3)` a few iterations ahead. Treat this as a last resort and measure — a bad prefetch wastes bandwidth and evicts useful lines, and the hardware is usually smarter than you.

**Try next:** build both matrix binaries above, run each under `perf stat -e cache-misses,LLC-load-misses`, and confirm the column-major version's LLC misses jump by an order of magnitude while instruction counts stay identical — then shrink `N` until the whole matrix fits in L2 and watch the gap vanish, proving it was the cache all along, not the arithmetic.
