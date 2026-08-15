---
title: "Cache-friendly code: the memory hierarchy under program control"
date: 2026-08-10
track: linux-tools
summary: "A core can retire several instructions per cycle but stalls for a hundred-plus cycles on a load that reaches DRAM. Data layout is the part of the memory hierarchy a program controls directly. This covers the L1/L2/L3/DRAM latency ladder, cache lines and locality, array-of-structs versus struct-of-arrays, why linked lists lose to arrays, false sharing, and prefetching, with a row-major versus column-major benchmark measurable under perf."
reading_time: 7
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

**Gist.** A modern core can retire several instructions per cycle, but a load that misses every cache level and reaches dynamic random-access memory (DRAM) costs on the order of a hundred-plus cycles, so one pointer dereference can stall the equivalent of hundreds of instructions. The caches absorb that gap automatically, and the only lever a program has over them is **the layout and traversal order of its data**, because hardware placement and prefetch policies key off address patterns rather than source code. The cost of pulling that lever is that layouts tuned for one access pattern — dense field-at-a-time streaming, for instance — are worse for the opposite pattern, so the choice binds the data structure to a specific hot loop.

## The latency ladder

The absolute numbers vary by microarchitecture, frequency and working-set size; the ratios are the stable part. Measured on an Intel Skylake i7-6700 at approximately 4 GHz ([7-cpu.com](https://www.7-cpu.com/cpu/Skylake.html)):

| Level | Rough latency | Ballpark |
|-------|---------------|----------|
| L1 data cache | ~4 cycles | ~1 ns |
| L2 cache | ~12 cycles | ~3 ns |
| L3 (last-level cache, LLC) | ~42 cycles | ~10 ns |
| Main memory (DRAM) | ~42 cycles + ~51 ns | ~60 ns / ~250 cycles at 4 GHz |

The DRAM row is not an independent measurement: a DRAM access is reached only *after* the L3 lookup misses, so its cost is the L3 latency plus the memory-controller round trip. The widely circulated "[Latency Numbers Every Programmer Should Know](https://gist.github.com/jboner/2841832)" list rounds a different generation of silicon to L1 ≈ 0.5 ns, L2 ≈ 7 ns, main memory ≈ 100 ns — same shape, different hardware. The step from one level to the next is a factor of roughly three to four, and the steps compound to **about two orders of magnitude between L1 and DRAM**. A miss is therefore not a marginal tax; it is the difference between a load that costs nothing measurable and one during which the core has no work to retire.

## Cache lines and locality

Caches do not transfer bytes; they transfer **cache lines**. On x86-64 a line is 64 bytes, which `getconf LEVEL1_DCACHE_LINESIZE` reports. Touching one byte causes the hardware to fetch the entire aligned 64-byte block containing it. Two properties follow, and they account for most of the observable variation:

- **Spatial locality.** Data adjacent to recently used data is already resident in the fetched line. A sequential walk over 4-byte `int`s or 8-byte pointers yields **16 or 8 elements per miss** respectively.
- **Temporal locality.** Data reused soon after its first touch is still resident. A loop whose working set fits in L1 pays the miss cost once.

Drepper's [*What Every Programmer Should Know About Memory*](https://people.freebsd.org/~lstewart/articles/cpumemory.pdf) develops this at length: the governing variable is how the access pattern maps onto lines, cache sets and pages, not the instruction count of the loop.

## Sequential versus random traversal

This is why traversal of a contiguous array outperforms traversal of a linked list even though both are O(n). In an array, each 64-byte line serves several consecutive elements, and the stride is constant, so the hardware prefetcher can issue the next fetch before the program asks for it. A linked list scatters nodes across the heap, and each `node = node->next` is a **dependent load**: the address of the next access is not known until the current load completes. The prefetcher has nothing to extrapolate from, and the misses serialize rather than overlap — the core pays roughly the full DRAM latency per hop instead of amortising several outstanding misses against each other. Asymptotic notation counts operations; it does not record that one operation class is two orders of magnitude more expensive than another.

## Array of structs versus struct of arrays

Consider a large particle set and a hot loop that touches only position and velocity:

```c
// Array of Structs (AoS): fields of one particle are interleaved in memory
struct Particle { float x, y, z; float vx, vy, vz; int id; char tag[16]; };
struct Particle parts[N];               // 44 bytes of fields at 4-byte alignment

for (int i = 0; i < N; i++)
    parts[i].x += parts[i].vx * dt;     // the line fetch drags in the whole struct
```

Each iteration reads `x` and `vx` — 8 bytes — but the fetch delivers 64 bytes including `z`, `id` and `tag[]`. Those unused bytes consume DRAM bandwidth and occupy cache capacity that other lines could hold. **Eight of the 64 bytes the fetch delivers are what the loop asked for**; the rest ride along because they share the line with them.

**Struct of Arrays (SoA)** stores each field in its own dense array:

```c
struct Particles { float x[N], y[N], z[N], vx[N], vy[N], vz[N]; };
struct Particles p;

for (int i = 0; i < N; i++)
    p.x[i] += p.vx[i] * dt;             // two dense streams, both fully consumed
```

Every byte of each fetched line is now consumed by the loop, and each array is a contiguous run of identically typed values, which is the form single-instruction-multiple-data (SIMD) vectorisation requires. Agner Fog's [*Optimizing software in C++*](https://www.agner.org/optimize/optimizing_cpp.pdf) covers the layout and vectorisation mechanics. The transformation is directional, not universally better: a loop that touches *every* field of *one* object performs one line fetch under AoS and one fetch per field array under SoA. The access pattern decides.

## Benchmark: row-major versus column-major traversal

The clearest demonstration traverses a matrix in memory order and against it. C stores arrays row-major, so `m[i][j]` and `m[i][j+1]` are adjacent, while `m[i][j]` and `m[i+1][j]` are N floats apart.

```c
#include <stdlib.h>
#define N 8192
static float m[N][N];   // 256 MB — larger than any cache level

int main(void) {
    double sum = 0;
#ifdef COLMAJOR
    for (int j = 0; j < N; j++)
        for (int i = 0; i < N; i++)   // stride N*4 = 32768 B: a new line every element
            sum += m[i][j];
#else
    for (int i = 0; i < N; i++)
        for (int j = 0; j < N; j++)   // stride 4 B: 16 hits per 64-byte line
            sum += m[i][j];
#endif
    return (int)sum;
}
```

The two loops perform identical arithmetic and, to a first approximation, identical instruction counts. The row-major order consumes each 64-byte line 16 times before advancing. The column-major order consumes one float per line, and by the time the traversal returns to the neighbouring column that line has long been evicted, since one full inner pass touches 8192 lines spread over 256 MB. The result approaches **one miss per access**. The column-major loop therefore runs several times slower, with the gap widening as the working set exceeds L3 further; the exact factor depends on the machine's DRAM latency and prefetch behaviour, so it has to be measured rather than quoted.

## Measuring with perf

Defensible numbers come from hardware counters:

```sh
gcc -O2 -o rowmajor  bench.c
gcc -O2 -DCOLMAJOR -o colmajor bench.c

perf stat -e cache-references,cache-misses,L1-dcache-loads,L1-dcache-load-misses,LLC-load-misses \
    ./rowmajor
perf stat -e cache-references,cache-misses,L1-dcache-loads,L1-dcache-load-misses,LLC-load-misses \
    ./colmajor
```

The row-major run reports an L1 load-miss rate near the floor the line size sets: one compulsory miss per 16 floats, so a few percent. The column-major run reports `LLC-load-misses` an order of magnitude higher and a miss rate approaching one per access, with wall-clock time tracking it. The ratio `cache-misses / cache-references` is the summary figure.

## Alignment, padding and false sharing

Two effects live at the line boundary. **Alignment**: a hot structure that straddles two lines requires two fetches rather than one; aligning it to 64 bytes (`alignas(64)`, or `__attribute__((aligned(64)))` in GCC and Clang) keeps it within a single line. **False sharing**: two threads writing *distinct* variables that happen to occupy the same 64-byte line force the cache-coherence protocol to transfer that line between cores on every write, because coherence is tracked per line, not per variable. There is no logical conflict, yet throughput degrades as threads are added. The remedy is the same knob — pad or align per-thread data so each written variable owns a line. The symptom does not appear in a flame graph, since the stall is attributed to the innocent-looking store; a counter-based hunt is described in [perf c2c: find the cacheline two threads are fighting over](/articles/linux-tools/2026-07-31-perf-c2c-false-sharing).

## Prefetching

The hardware prefetcher observes the access stream and, on detecting a linear stride, fetches ahead so the line is resident before the load issues. This is the mechanism behind the sequential-versus-random gap: a predictable stride lets the prefetcher overlap latency that a linked list's unpredictable hops leave exposed. Where a pattern is regular but not detected, `__builtin_prefetch(ptr, 0, 3)` issued several iterations ahead can hint it explicitly. A mistimed or unnecessary prefetch consumes bandwidth and evicts live lines, so the hint requires measurement to justify.

## Pitfalls

- **Adopting SoA for a loop that touches every field.** Symptom: the transformation slows the code. Cause: one object spread across six arrays costs one line fetch per array instead of the single fetch AoS needed.
- **Quoting `cache-misses` without `cache-references`.** Symptom: two runs appear comparable. Cause: the absolute miss count scales with total accesses; only the ratio characterises locality.
- **Benchmarking a matrix that fits in cache.** Symptom: row-major and column-major times converge. Cause: once the working set is resident, stride no longer determines whether an access misses.
- **Padding a struct to 64 bytes to fix false sharing while the array of those structs stays hot.** Symptom: the coherence traffic disappears but single-threaded traversal slows. Cause: padding lowers the number of useful elements per fetched line.
- **Assuming `alignas(64)` survives heap allocation.** Symptom: over-aligned types behave as if unaligned. Cause: allocation paths that predate C11 `aligned_alloc` / C++17 aligned `operator new` return memory aligned only to the platform's default.
- **Adding `__builtin_prefetch` without measuring.** Symptom: throughput drops. Cause: the prefetch competes for bandwidth and cache capacity with lines the loop is still using.
- **Treating the DRAM latency row as parallel to the cache rows.** Symptom: a memory access budget that is too optimistic. Cause: a DRAM access is charged the L3 lookup as well, since it is only reached after that lookup misses.
