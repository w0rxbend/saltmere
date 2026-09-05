---
title: "Hunting Heap Leaks and Fragmentation on the ESP32 with heap_caps and Heap Tracing"
date: 2026-08-27
track: iot-embedded
summary: "malloc on the ESP32 is a facade over a set of independent heaps, one per contiguous RAM region, each tagged with capability flags such as MALLOC_CAP_DMA and MALLOC_CAP_8BIT. This article walks the capability-based allocator, explains why heap_caps_get_free_size and heap_caps_get_largest_free_block diverge under fragmentation — the reason a node reporting 80 KB free can still fail a 4 KB TLS allocation — and shows how HEAP_TRACE_LEAKS plus heap poisoning turns a slow leak in a long-running sensor node into an attributable call stack, at a quantifiable cost in RAM and CPU."
reading_time: 8
tags: [esp32, esp-idf, heap, fragmentation, memory-debugging, freertos]
sources:
  - title: "Heap Memory Allocation — ESP-IDF Programming Guide (stable)"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/system/mem_alloc.html"
  - title: "Heap Memory Debugging — ESP-IDF Programming Guide (stable)"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/system/heap_debug.html"
---

**Gist.** The ESP32 has no single heap: each contiguous region of internal RAM carries its own heap, tagged with capability flags, and `malloc()` is a thin dispatcher over them. Two consequences follow. First, "free memory" is a set of per-region free lists, so total free bytes and the largest single allocatable block diverge as fragmentation grows — a device can report tens of kilobytes free and still fail a single multi-kilobyte allocation. Second, because every allocation passes through one component, ESP-IDF can instrument it: heap tracing in `HEAP_TRACE_LEAKS` mode records the call stack of every allocation still outstanding, and heap poisoning plants canary words that convert silent buffer overflows into detected corruption. The cost is per-allocation RAM overhead for trace records and canaries, and a documented, substantial runtime penalty for comprehensive poisoning.

## One malloc, many heaps

The ESP32's internal RAM is not uniform. **Data RAM (DRAM)** hangs off the CPU's data bus and is byte-addressable; **instruction RAM (IRAM)** hangs off the instruction bus and normally admits only 32-bit aligned access; some regions (D/IRAM) can serve as either. External pseudo-static RAM (PSRAM), where fitted, is byte-addressable but reachable only through a cache and unusable for direct memory access (DMA). A single free list cannot describe this landscape, so ESP-IDF builds **one heap per contiguous memory region** on the `multi_heap` allocator and tags each heap with capability flags:

- `MALLOC_CAP_8BIT` — byte-addressable memory (DRAM, PSRAM).
- `MALLOC_CAP_32BIT` — memory that tolerates only 32-bit aligned word access (includes IRAM).
- `MALLOC_CAP_DMA` — memory a DMA peripheral can reach; **external PSRAM never qualifies**.
- `MALLOC_CAP_EXEC` — memory that can hold executable code.
- `MALLOC_CAP_SPIRAM` — external SPI RAM explicitly.
- `MALLOC_CAP_DEFAULT` — what plain `malloc()` asks for.

A call to `malloc(n)` becomes `heap_caps_malloc_default(n)`, and the allocator picks a heap whose capabilities satisfy the request, preferring regions so that scarce memory (DMA-capable internal DRAM, for instance) is not squandered on allocations that could live anywhere. Code with real constraints states them directly:

```c
// A buffer a peripheral will DMA into: must be internal, DMA-capable DRAM.
uint8_t *rx = heap_caps_malloc(1024, MALLOC_CAP_DMA);

// A large history buffer with no access constraints: push it to PSRAM
// so it never competes with DMA buffers for internal DRAM.
sample_t *hist = heap_caps_malloc(sizeof(sample_t) * 4096, MALLOC_CAP_SPIRAM);
```

`free()` remains universal — it locates the heap containing the pointer — so capability-allocated memory needs no matching capability-aware free.

## Why 80 KB free still fails a 4 KB allocation

`heap_caps_get_free_size(caps)` sums free bytes across every heap matching `caps`. `heap_caps_get_largest_free_block(caps)` reports the biggest single block any of those heaps can hand out. **Only the second number decides whether a given `malloc` succeeds**, and fragmentation drives them apart.

The mechanism is ordinary free-list arithmetic. A long-running sensor node interleaves allocations with different lifetimes: short-lived JSON buffers, medium-lived MQTT (Message Queuing Telemetry Transport) messages, long-lived driver state. When the short-lived objects are freed, the survivors pin the address space between them, and the free bytes exist only as many small gaps. A node can truthfully report 80 KB free — spread across, say, dozens of sub-kilobyte holes in several regions — while the largest contiguous hole in any byte-addressable heap is under 4 KB. The next Transport Layer Security (TLS) handshake, which needs multi-kilobyte contiguous record buffers, then fails with `ESP_ERR_NO_MEM` on a device whose dashboard says memory is plentiful. The multi-heap split sharpens the effect: part of that "free" total may sit in 32-bit-only IRAM or in PSRAM, which a `MALLOC_CAP_DMA` or internal-only request cannot use at all.

The consequence for monitoring is direct: a fleet health metric must export **both** numbers, per capability class, plus `minimum_free_bytes` — the lifetime low-watermark the allocator tracks — because total-free alone cannot distinguish a healthy node from one a day away from allocation failure. `heap_caps_get_info()` returns all of these in one `multi_heap_info_t`, including allocated- and free-block counts, from which a fragmentation ratio (largest free block over total free) is one line of arithmetic.

## HEAP_TRACE_LEAKS: from slow drift to a call stack

A leak on a sensor node rarely announces itself; `minimum_free_bytes` drifts down over days. Because every allocation funnels through the heap component, ESP-IDF can record who allocated what. **Standalone heap tracing** stores records in a caller-provided buffer:

```c
#define NUM_RECORDS 100
static heap_trace_record_t trace[NUM_RECORDS];  // in internal RAM

void app_main(void) {
    ESP_ERROR_CHECK(heap_trace_init_standalone(trace, NUM_RECORDS));
    // ... bring up sensors, network ...
    ESP_ERROR_CHECK(heap_trace_start(HEAP_TRACE_LEAKS));
    run_measurement_cycles(1000);          // the suspected leaky workload
    ESP_ERROR_CHECK(heap_trace_stop());
    heap_trace_dump();                     // records = allocations never freed
}
```

In `HEAP_TRACE_LEAKS` mode the trace behaves like a set: an allocation inserts a record, its `free` removes it, so **whatever remains after a workload that should be steady-state is, by construction, the leak**, each record carrying the allocation size, address, and a call stack whose depth is set by `CONFIG_HEAP_TRACING_STACK_DEPTH` (up to 32 frames, at 8 bytes of record space per frame). `HEAP_TRACE_ALL` instead retains every record, including where freed allocations were released — more volume, useful for fragmentation studies rather than leak hunts. For workloads whose live-allocation count exceeds any affordable buffer, host-based tracing (`heap_trace_init_tohost()`) streams records over JTAG (Joint Test Action Group debug port) instead; `CONFIG_HEAP_TRACE_HASH_MAP` speeds record lookup when counts grow large.

The trap in standalone mode is buffer exhaustion: once the record buffer fills, new allocations go unrecorded, and the dump undercounts precisely the busiest code paths. The dump reports whether records were dropped; a leak hunt with a saturated buffer proves nothing.

## Poisoning: canaries for overflows, patterns for stale pointers

Tracing finds memory that is never freed. It says nothing about memory that is written *past*. Heap poisoning covers that class, in three configurable levels:

- **Basic** (default): no canaries; corruption of the allocator's own metadata is caught by assertions when the damaged block is next manipulated.
- **Light impact**: every allocation is bracketed by a **head canary `0xABBA1234` and a tail canary `0xBAAD5678`**. A buffer overflow of even one byte tramples the tail word, and the damage is detected when the block is freed or when `heap_caps_check_integrity_all()` walks the heaps. Cost: a few words of overhead per allocation and a check on free.
- **Comprehensive**: additionally fills newly allocated memory with `0xCE` and freed memory with `0xFE`. Reads of uninitialised buffers return the recognisable `0xCECECECE`; writes through stale pointers disturb the `0xFE` fill of freed blocks and are flagged. The documentation describes the runtime impact as **substantial** — every allocation and free now memsets its block — which is why comprehensive poisoning is a debugging configuration, not a shipping one. No published Espressif benchmark states a percentage figure, so the honest sizing method is to measure the target workload under each level.

`heap_caps_check_integrity_all()` can run periodically from a low-priority task in a soak test, converting "the node rebooted overnight" into "the tail canary of a 512-byte block allocated by the LoRa driver was corrupted at 03:12" — hours closer to the culprit than the eventual crash.

## Pitfalls

- Alerting on `heap_caps_get_free_size()` alone misses fragmentation death: allocation failures begin when `heap_caps_get_largest_free_block()` falls below the largest single request, which can happen with abundant total free memory.
- A `MALLOC_CAP_DMA` request can fail while PSRAM sits mostly empty, because external PSRAM is never DMA-capable and the internal DMA-capable heaps are exhausted independently.
- Freeing memory in `HEAP_TRACE_LEAKS` mode that was allocated before `heap_trace_start()` removes nothing from the trace, so a start point placed mid-lifecycle misattributes steady-state churn as leaks; start tracing only once the system reaches the state it should hold.
- A standalone trace buffer that fills up silently stops recording new allocations, so the busiest — usually leakiest — path is the one undercounted; the dump's dropped-record indication must be checked before trusting the result.
- Light-impact poisoning detects an overflow only when the block is freed or an integrity check runs, so a long-lived overflowed buffer can corrupt its neighbour long before detection; periodic `heap_caps_check_integrity_all()` shrinks that window.
- Comprehensive poisoning changes timing enough to mask race-dependent bugs while hunting memory bugs: the `0xCE`/`0xFE` fills slow allocation and free on every call, so a bug that reproduces in the field can vanish under the instrumented build.
- Trace records and canaries consume internal RAM themselves, lowering `minimum_free_bytes`; a node tuned near its memory limit can fail allocations under instrumentation that it survives in production, inverting the usual debug/release expectation.
