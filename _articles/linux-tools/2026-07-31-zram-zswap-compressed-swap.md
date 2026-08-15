---
title: "zram and zswap: Two Different Ways to Compress Swap"
date: 2026-07-31
track: linux-tools
summary: "zram is a compressed block device that lives entirely in RAM; zswap is a compressed cache in front of a real disk swap that tiers cold pages down under pressure. The two are routinely confused: this article separates the mechanisms, the sysfs knobs, and the kernel defaults that differ from distribution defaults."
reading_time: 6
tags: [linux, memory, swap, zram, zswap, kernel]
sources:
  - title: "zram: Compressed RAM-based block devices — Linux Kernel documentation"
    url: "https://docs.kernel.org/admin-guide/blockdev/zram.html"
  - title: "zswap — Linux Kernel documentation"
    url: "https://docs.kernel.org/admin-guide/mm/zswap.html"
  - title: "Debunking zswap and zram myths: when to use what — Chris Down"
    url: "https://chrisdown.name/2026/03/24/zswap-vs-zram-when-to-use-what.html"
  - title: "Workload-specific and memory pressure-driven zswap writeback — LWN.net"
    url: "https://lwn.net/Articles/953429/"
  - title: "systemd/zram-generator (Fedora's default zram swap)"
    url: "https://github.com/systemd/zram-generator"
---

**Gist.** Swapping a page to a rotating or solid-state disk costs orders of magnitude more time than touching random-access memory (RAM), so both zram and zswap interpose compression to keep evicted pages resident. zram is a compressed block device backed only by RAM, used as a swap device in its own right; zswap is a compressed write-back cache that sits in front of an existing on-disk swap device and tiers cold pages down to it under pressure. Both trade processor cycles and a compressed memory pool for reduced input/output (I/O), and choosing the wrong one inverts the least-recently-used (LRU) ordering the kernel is trying to maintain.

## What each mechanism is

**zram** is a compressed block device that lives entirely in RAM, exposed as `/dev/zram0`. Pages written to it are compressed and kept in memory. Used as swap, it yields swap that never touches disk, at the cost of processor time and a memory multiplier bounded by the achieved compression ratio. It has **a hard size limit (`disksize`) and no automatic eviction**: once the device is full, further swap stores to it fail, and the kernel must find another swap device or reclaim elsewhere.

**zswap** is a compressed write-back cache in front of a real disk swap. A page on its way out to swap is compressed into a RAM pool first; under pressure zswap **writes the cold entries back** to the backing disk swap and frees their pool space. It therefore requires an already-active on-disk swap device to be useful, and it provides a compressed hot tier with genuine disk overflow behind it.

The structural difference is the presence of an overflow path. zram has none by default, so its capacity is a wall; zswap has one, so its pool is a cache whose occupancy is bounded by policy rather than by failure.

## Why the two should not be stacked

Running zram swap **and** a disk swap device together causes **LRU inversion**: the kernel pushes genuinely cold pages to the slow disk while less-cold pages remain compressed in fast RAM. The swap subsystem picks a device by priority and by free space, not by page age, so age ordering across two devices of different speeds is not preserved. Running zram and zswap over the same swap path is likewise a misconfiguration: zswap would be compressing pages destined for a device that already compresses them.

Chris Down's post argues for **preferring zswap in the general case** and reaching for zram only when there is no persistent storage at all — embedded systems, diskless nodes, live media — or when a security requirement forbids writing swapped pages to disk. zram is nonetheless the common choice on several platforms: Android and ChromeOS use it, and Fedora ships zram swap by default through the systemd `zram-generator`.

## Configuring zram

The `zramctl` tool from util-linux is the direct path:

```bash
zramctl --find --size 4G --algorithm zstd     # -> /dev/zram0
mkswap /dev/zram0
swapon --priority 100 /dev/zram0              # high priority: used before lower-priority devices
zramctl                                        # NAME ALGORITHM DISKSIZE DATA COMPR TOTAL
```

The `zramctl` output separates **DATA** (uncompressed bytes stored), **COMPR** (compressed bytes) and **TOTAL** (compressed bytes plus allocator overhead). The ratio that matters for capacity planning is DATA divided by TOTAL, not DATA divided by COMPR, because the allocator's per-object overhead is charged against real memory.

The same device can be driven through sysfs for scripting:

```bash
modprobe zram num_devices=1
echo zstd > /sys/block/zram0/comp_algorithm    # lzo-rle is the usual default; zstd packs denser
echo 4G   > /sys/block/zram0/disksize
mkswap /dev/zram0 && swapon /dev/zram0
```

The ordering is load-bearing: **`comp_algorithm` must be set before `disksize`**, since writing `disksize` initialises the device and fixes its parameters.

Two later additions extend the model. Kernels that enable it support **recompression** — a secondary, slower and denser algorithm applied to pages already stored, selected by state such as idleness:

```bash
echo "algo=zstd priority=1" > /sys/block/zram0/recomp_algorithm
echo "type=idle priority=1" > /sys/block/zram0/recompress
```

Separately, zram can **write back** incompressible or idle pages to a real backing device, which restores an overflow path that the default configuration lacks:

```bash
echo /dev/sdaX > /sys/block/zram0/backing_dev
echo idle      > /sys/block/zram0/writeback
```

The `writeback_limit` attribute caps the number of pages this path may write, bounding the write volume the mechanism can inflict on the backing device.

## Configuring zswap

zswap is controlled by module parameters under `/sys/module/zswap/parameters/`, and requires disk swap to be active already:

```bash
echo 1        > /sys/module/zswap/parameters/enabled
echo zstd     > /sys/module/zswap/parameters/compressor
echo zsmalloc > /sys/module/zswap/parameters/zpool
echo 20       > /sys/module/zswap/parameters/max_pool_percent
echo Y        > /sys/module/zswap/parameters/shrinker_enabled
```

Two details are commonly misread. First, the **upstream default compressor is `lzo`, not `zstd`**; the widespread claim that zstd is the default reflects a per-distribution build choice. The setting should be made explicit and verified with `cat /sys/module/zswap/parameters/compressor`. Second, the pool-allocator situation changed: **`zbud` and `z3fold` have been removed from recent kernels**, leaving `zsmalloc` as the only pool backend there.

The remaining parameters define the pool's admission state machine. **`max_pool_percent` (default 20)** caps the fraction of RAM the compressed pool may occupy. **`accept_threshold_percent` (default 90)** supplies hysteresis: once the cap is hit, zswap stops accepting new stores and resumes only after occupancy falls back below that fraction of the cap. Without the hysteresis, occupancy hovering at the limit would oscillate between accepting and rejecting on every store. The dynamic, memory-pressure-driven writeback **shrinker landed in Linux 6.8 and is off by default**, which is why `shrinker_enabled` is set above; the LWN.net article listed in the sources describes that work.

Persisting the configuration across reboots requires the kernel command line rather than sysfs, because the module parameters are consulted at initialisation:

```
zswap.enabled=1 zswap.compressor=zstd zswap.zpool=zsmalloc zswap.shrinker_enabled=1
```

Operation is confirmed through debugfs:

```bash
grep -r . /sys/kernel/debug/zswap/     # stored_pages, pool_total_size, reject_*
# compression ratio ≈ stored_pages * 4096 / pool_total_size
```

The `reject_*` counters are the diagnostic that distinguishes "zswap is enabled but idle" from "zswap is enabled and refusing stores", the latter indicating the pool cap or an allocation failure rather than an absence of swap activity.

## An experiment that separates the two

On a spare virtual machine with RAM capped low, a memory-hungry workload such as `stress-ng --vm 2 --vm-bytes 90% --timeout 60s` can be run three ways: plain disk swap, zram swap, and disk swap with zswap enabled. `zramctl` and the debugfs counters report pool occupancy and compression ratio; `vmstat 1` reports `si` and `so`, the swap-in and swap-out page rates. Deliberately misconfiguring the system — zram and a disk swap device at equal priority — surfaces the LRU inversion as disk I/O appearing while compressed RAM capacity remains free.

## Pitfalls

- Writing `comp_algorithm` after `disksize` leaves the device on the previous algorithm: the write is rejected or ignored because `disksize` has already initialised the device.
- A full zram device does not evict anything; swap stores fail and the kernel falls through to another device or to direct reclaim, because zram has no automatic eviction and no backing device unless `backing_dev` is configured.
- Enabling zram swap alongside an existing disk swap produces disk I/O under moderate pressure: device selection follows priority and free space, not page age, so cold pages reach the disk while warmer pages stay compressed in RAM.
- Assuming `zstd` is active under zswap yields the compression ratio of `lzo`, since the upstream default compressor is `lzo` and the zstd default is a distribution build choice.
- Configuring `zpool=zbud` or `zpool=z3fold` fails on kernels from which those allocators have been removed; only `zsmalloc` remains there.
- Setting zswap parameters through sysfs alone loses them at reboot; the values must be on the kernel command line to apply at module initialisation.
- Leaving `shrinker_enabled` unset means no memory-pressure-driven writeback, so the pool fills to `max_pool_percent` and then rejects stores until occupancy drops below `accept_threshold_percent`.
- Reading the compression ratio from `zramctl`'s DATA and COMPR columns overstates capacity, because allocator overhead is counted only in TOTAL.
