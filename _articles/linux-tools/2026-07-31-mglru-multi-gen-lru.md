---
title: "MGLRU: Multi-Generational Page Reclaim in the Linux Kernel"
date: 2026-07-31
track: linux-tools
summary: "Why the classic active/inactive LRU aged pages badly and burned CPU under pressure, and how MGLRU's generations plus page-table Accessed-bit scanning replace it. Merged in 6.1, tunable through sysfs and debugfs."
reading_time: 5
tags: [mglru, memory-management, kernel, page-reclaim, lru, performance]
sources:
  - title: "Multi-Gen LRU — The Linux Kernel documentation"
    url: "https://docs.kernel.org/admin-guide/mm/multigen_lru.html"
  - title: "Merging the multi-generational LRU [LWN.net]"
    url: "https://lwn.net/Articles/894859/"
  - title: "Reconsidering the multi-generational LRU [LWN.net]"
    url: "https://lwn.net/Articles/1060967/"
  - title: "MGLRU Merged For Linux 6.1 — Phoronix"
    url: "https://www.phoronix.com/news/MGLRU-In-Linux-6.1"
  - title: "MGLRU — Linux Kernel Internals"
    url: "https://kernel-internals.org/mm/mglru/"
---

For decades Linux reclaimed pages with two lists per node: active and inactive. Pages moved between them based on the PTE Accessed bit, but the design had two structural problems. First, aging accuracy was poor — a page had only a coarse "active or not" status, so the kernel could not tell a barely-warm page from a genuinely hot one, and working sets got evicted while cold cruft lingered. Second, finding out which pages were accessed meant reverse-mapping (rmap): for each candidate page, walk every process that maps it. Under memory pressure that scan constantly switches between different processes' page tables, thrashing the CPU cache exactly when the system can least afford it.

MGLRU (Multi-Gen LRU), written by Yu Zhao at Google and merged into **Linux 6.1** in October 2022, replaces the two lists with a different model. Google had already shipped it on Chrome OS and Android before mainlining.

## Generations instead of two lists

MGLRU sorts pages into **generations** — buckets that reflect age, i.e. how long since a page was last accessed. By default there are up to four generations per node, tracked separately for anonymous and file pages. Two sequence counters bound them: `max_seq` marks the youngest generation and `min_seq` the oldest.

Two operations drive everything. **Aging** produces new generations: it scans for recently accessed pages and promotes them to `max_seq`, then bumps the counter to open a fresh youngest generation. **Eviction** consumes the oldest one: it reclaims folios from `min_seq`, and once that generation empties, `min_seq` advances. A finer-grained ladder of generations is a far better age signal than a single active/inactive bit.

## Scanning page tables, not rmap

The bigger win is how MGLRU finds accessed pages. Instead of rmap-walking each page back to its mappers, aging walks **process page tables directly**, reading and clearing the Accessed bit in PTEs in bulk. Sparse address spaces would make that wasteful, so MGLRU keeps a **Bloom filter** to skip page-table pages that hold few active entries. The same refault-tracking machinery detects pages that were evicted and immediately faulted back in, and reinserts them into a protected generation so a mis-sized working set is not repeatedly thrown out.

## Enabling and checking it

MGLRU needs `CONFIG_LRU_GEN=y`; `CONFIG_LRU_GEN_ENABLED=y` makes it the default at boot. Most desktop distros (Arch, Fedora, Ubuntu) now ship it enabled. The runtime control is a bitmask in sysfs:

```bash
# Is MGLRU active? Non-zero bitmask means yes.
cat /sys/kernel/mm/lru_gen/enabled        # e.g. 0x0007

# Turn everything on (equivalent to writing 0x0007)
echo y | sudo tee /sys/kernel/mm/lru_gen/enabled
```

The bits are independent features:

| Bit | Meaning |
|--------|---------|
| `0x0001` | Core multi-gen LRU |
| `0x0002` | Batch-clear Accessed bits in leaf PTEs |
| `0x0004` | Clear Accessed bits in non-leaf (higher-level) PTEs |

So `echo 5` enables the core plus non-leaf clearing (`0x0001 | 0x0004`). There is also a thrash guard, `min_ttl_ms`: it prevents evicting the working set until it is at least that old. It defaults to `0` (off); `1000` noticeably cuts UI stalls, but large values like `3000` raise the risk of a premature OOM kill.

```bash
echo 1000 | sudo tee /sys/kernel/mm/lru_gen/min_ttl_ms
```

## The debugfs interface

With debugfs mounted, `/sys/kernel/debug/lru_gen` exposes aging and eviction directly — useful for working-set estimation and **proactive reclaim**. Commands take `memcg_id` and `node_id`. Force a new generation (age memcg 0 on node 0), then reclaim everything at or below a given generation:

```bash
# Age: create a new max generation for memcg 0, node 0
echo '+ 0 0 0 1 1' | sudo tee /sys/kernel/debug/lru_gen

# Evict generations <= min_gen_nr, swappiness 200, up to 4096 pages
echo '- 0 0 0 200 4096' | sudo tee /sys/kernel/debug/lru_gen
```

Build with `CONFIG_LRU_GEN_STATS=y` and `/sys/kernel/debug/lru_gen_full` reports per-generation timestamps and page counts for verifying that aging tracks your workload.

## Where it stands in 2026

MGLRU is stable and widely deployed but is still an *alternative* to the classic LRU, not the sole mechanism — the dual code paths coexist. A March 2026 LWN discussion aired real complaints: anonymous pages tend to cling to the youngest generations while file pages get reclaimed too aggressively (and `swappiness` alone does not fix it), plus regressions on some workloads and thin maintenance after Google stepped back. Maintainers, including Axel Rasmussen, committed to working through these in 2026, with the eventual goal of making MGLRU the default and retiring the old two-list scheme. For now, treat it as production-ready but worth measuring on your own workload before flipping defaults.

**Try next:** on a memory-pressured box, `cat /sys/kernel/mm/lru_gen/enabled` to confirm the bitmask, then toggle it off (`echo 0`) and on (`echo y`) while watching `sar -B 1` or `/proc/vmstat` fields `pgscan_kswapd`/`pgsteal_kswapd` — compare pages-scanned-per-page-reclaimed to see the aging efficiency difference directly.
