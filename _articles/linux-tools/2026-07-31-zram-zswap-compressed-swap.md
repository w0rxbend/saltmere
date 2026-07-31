---
title: "zram and zswap: Two Very Different Ways to Compress Your Swap"
date: 2026-07-31
track: linux-tools
summary: "zram is a compressed block device that lives entirely in RAM; zswap is a compressed cache in front of a real disk swap that tiers cold pages down under pressure. They sound alike and are not — here's which to reach for, the sysfs knobs that matter, and the kernel-default gotchas."
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

The names rhyme and both compress memory, so people treat zram and zswap as interchangeable. They are not, and picking the wrong one gives you the *opposite* of what you wanted — cold pages pinned in fast RAM while your hot working set gets shoved onto a slow disk. Here's the actual distinction and how to drive each one.

## What each thing is

**zram** is a compressed block device that lives *entirely in RAM* — `/dev/zram0`. Pages written to it are compressed and kept in memory; used as swap, it means "swap that never touches disk, at the cost of some CPU and a lower effective memory multiplier." It has a hard size limit and **no automatic eviction**: when the device is full, it's full.

**zswap** is a compressed *writeback cache in front of a real disk swap*. Pages on their way out to swap get compressed into a RAM pool first, and under pressure zswap **tiers the cold ones down** to the backing disk. It needs an existing on-disk swap device to be useful, and it gives you a compressed hot tier plus real disk overflow.

The rule that follows: **don't stack them.** Running zram *and* a disk swap causes LRU inversion — the kernel pushes your genuinely-cold pages to the slow disk while cold-ish pages sit compressed in fast RAM. And don't run zram and zswap over the same swap. Kernel developer Chris Down's guidance is blunt: prefer zswap in general; reach for zram only when there's no persistent storage at all (embedded, diskless, live media) or a security reason to never write swap to disk. That's why phones — Android, ChromeOS — use zram, and why Fedora ships zram swap by default via the systemd `zram-generator`.

## Setting up zram

The `zramctl` tool from util-linux is the friendly path:

```bash
zramctl --find --size 4G --algorithm zstd     # -> /dev/zram0
mkswap /dev/zram0
swapon --priority 100 /dev/zram0              # high priority: use it first
zramctl                                        # NAME ALGORITHM DISKSIZE DATA COMPR TOTAL
```

Or drive the sysfs interface directly for scripting:

```bash
modprobe zram num_devices=1
echo zstd > /sys/block/zram0/comp_algorithm    # lzo-rle is the usual default; zstd packs denser
echo 4G   > /sys/block/zram0/disksize
mkswap /dev/zram0 && swapon /dev/zram0
```

Two newer tricks worth knowing. Since Linux 6.1 zram supports **recompression** — a secondary, slower-but-denser algorithm applied to idle pages:

```bash
echo "algo=zstd priority=1" > /sys/block/zram0/recomp_algorithm
echo "type=idle priority=1" > /sys/block/zram0/recompress
```

And zram can **write back** incompressible or idle pages to a real backing device (`echo /dev/sdaX > /sys/block/zram0/backing_dev`, then `echo idle > .../writeback`), rate-limited via `writeback_limit`.

## Setting up zswap

zswap is module parameters under `/sys/module/zswap/parameters/`, and it needs disk swap already active:

```bash
echo 1        > /sys/module/zswap/parameters/enabled
echo zstd     > /sys/module/zswap/parameters/compressor
echo zsmalloc > /sys/module/zswap/parameters/zpool
echo 20       > /sys/module/zswap/parameters/max_pool_percent
echo Y        > /sys/module/zswap/parameters/shrinker_enabled
```

Two facts that trip people up. First, the **upstream default compressor is `lzo`, not zstd** — the "zstd is the default" claim you'll read is a per-distro build choice. Set it explicitly and verify with `cat /sys/module/zswap/parameters/compressor`. Second, the pool allocator story changed: `zbud` and `z3fold` were **removed in Linux 6.15**, so `zsmalloc` is now effectively the only pool backend. Other knobs that matter: `max_pool_percent` (default 20 — cap on RAM the compressed pool may take) and `accept_threshold_percent` (default 90 — hysteresis before it resumes accepting stores after hitting the cap). The dynamic, pressure-driven writeback **shrinker** landed in 6.8 but is off by default, hence the `shrinker_enabled` line above.

To persist zswap across boots, put it on the kernel command line instead of sysfs:

```
zswap.enabled=1 zswap.compressor=zstd zswap.zpool=zsmalloc zswap.shrinker_enabled=1
```

Then confirm it's actually working via debugfs:

```bash
grep -r . /sys/kernel/debug/zswap/     # stored_pages, pool_total_size, reject_*
# compression ratio ≈ stored_pages * 4096 / pool_total_size
```

**Try next:** On a spare VM, cap RAM low and run a memory-hungry workload (`stress-ng --vm 2 --vm-bytes 90% --timeout 60s`) three ways: plain disk swap, zram swap, and disk swap with zswap enabled. Watch `zramctl` / the debugfs stats and `vmstat 1` for `si`/`so`, and compare how far each pushes off before thrashing. Then deliberately misconfigure — zram *and* disk swap at equal priority — and watch the LRU inversion show up as disk I/O that shouldn't be there.
