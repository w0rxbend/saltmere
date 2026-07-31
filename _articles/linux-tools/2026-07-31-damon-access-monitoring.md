---
title: "DAMON and DAMOS: Access-Aware Memory Management in the Linux Kernel"
date: 2026-07-31
track: linux-tools
summary: "How DAMON builds access-frequency heatmaps with workload-independent overhead, and how DAMOS turns them into automatic pageout, LRU sorting, and hugepage actions."
reading_time: 6
tags: [linux, kernel, memory, damon, damos, tiered-memory]
sources:
  - title: "DAMON Design — The Linux Kernel documentation"
    url: "https://docs.kernel.org/mm/damon/design.html"
  - title: "DAMON sysfs Interface — Detailed Usage"
    url: "https://docs.kernel.org/admin-guide/mm/damon/usage.html"
  - title: "Getting Started with DAMON — The Linux Kernel documentation"
    url: "https://docs.kernel.org/admin-guide/mm/damon/start.html"
  - title: "damo: DAMON user-space tool"
    url: "https://github.com/damonitor/damo"
  - title: "LRU-list manipulation with DAMON [LWN.net]"
    url: "https://lwn.net/Articles/905370/"
---

Knowing which pages a workload actually touches is the hard part of memory management. Page-table scanning costs scale with resident set size, so the naive approach gets more expensive exactly when memory pressure makes it most valuable. DAMON, mainline since Linux 5.15, sidesteps that with a sampling design whose overhead is bounded independent of workload size.

## Region-based sampling

DAMON groups adjacent pages assumed to share access frequency into a *region*, then samples a single page per region each sampling interval by checking and clearing its PTE Accessed bit. Two intervals drive it: the **sampling interval** (default 5ms) decides how often a region's representative page is probed, and the **aggregation interval** (default 100ms) is the window over which per-region access counts (`nr_accesses`) accumulate before being reported and reset. The docs recommend the sample interval be roughly 1/20th of the aggregation interval.

The trick that bounds cost is the region count, not the page count. You set `min`/`max` region bounds; overhead is proportional to the number of regions, so a 4KiB process and a 4TiB process cost the same to monitor if both use, say, 10–1000 regions.

## Adaptive regions

A fixed partitioning would be wrong the moment the workload shifts, so DAMON continuously **merges and splits** regions. Adjacent regions whose access frequencies are similar merge (as long as the result stays under a size threshold); regions whose sampled pages disagree split apart. Over a few aggregation intervals the partitioning converges so that each region genuinely contains pages of similar temperature — the assumption the sampling relies on — while staying inside the region-count budget.

## Virtual vs physical monitoring

DAMON's operations layer is pluggable. The **vaddr** operations set monitors a specific process's virtual address space (following its mappings), **fvaddr** monitors fixed virtual ranges, and **paddr** monitors the physical address space directly, using rmap to walk every page table mapping an address. Physical monitoring is what makes DAMON usable for system-wide, cross-process tiering — it sees pages regardless of which process owns them.

## DAMOS: acting on the heatmap

Monitoring is only half the story. DAMOS (DAMON-based Operation Schemes) lets you attach an **action** to an **access pattern**. A pattern is min/max bounds on three axes: region size, `nr_accesses`, and **age** (how many aggregation intervals the pattern has persisted). Any region matching all three gets the action applied.

Actions include `pageout` (reclaim), `hugepage`/`nohugepage` (advise THP collapse/split, vaddr only), `lru_prio`/`lru_deprio` (move pages toward the hot or cold end of the LRU lists, paddr only), `migrate_hot`/`migrate_cold` for tiered memory, and `stat` for dry-run measurement.

Crucially, DAMOS is throttled by **quotas** — cap the time or bytes it may touch per interval — and when the quota binds, DAMOS prioritizes regions by a weighted score over age and frequency, acting on the coldest/oldest first. **Watermarks** (e.g. `free_mem_rate`) auto-activate a scheme only under pressure, and **filters** exclude classes like anonymous or young pages.

## Driving it

The sysfs interface lives at `/sys/kernel/mm/damon/admin/`. Raw echoes work, but the `damo` tool is the practical front end. Record a physical-address heatmap and view it:

```bash
sudo damo record paddr        # writes ./damon.data
sudo damo report heatmap --resol 15 80
sudo damo report wss          # working-set-size distribution
```

Proactively page out memory idle for at least 5 seconds, capped so it never runs away:

```bash
sudo damo start \
    --damos_action pageout \
    --damos_access_rate 0% 0% \
    --damos_age 5s max \
    --damos_quota_interval 1s \
    --damos_quota_space 200MB
```

The equivalent scheme in raw sysfs sets `schemes/0/action` to `pageout`, fills `access_pattern/{sz,nr_accesses,age}/{min,max}`, and writes `quotas/ms`, `quotas/bytes`, and `quotas/reset_interval_ms` before `echo on > kdamonds/0/state`.

That combination — bounded-overhead monitoring plus quota-limited, age-gated actions — is what makes DAMON safe to leave running in production as a proactive reclaim or tiering engine.

**Try next:** run `sudo damo record $(pidof your_service)` for 30 seconds, then `damo report heatmap` to see which regions of your own workload are cold enough to reclaim.
