---
title: "DAMON and DAMOS: Access-Aware Memory Management in the Linux Kernel"
date: 2026-07-31
track: linux-tools
summary: "How DAMON builds access-frequency heatmaps with workload-independent overhead, and how DAMOS turns them into automatic pageout, LRU sorting, and hugepage actions."
reading_time: 7
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

**Gist.** Memory management needs to know which pages a workload touches, but exhaustive page-table scanning costs scale with resident set size, so the method grows most expensive precisely when memory pressure makes it most valuable. DAMON (Data Access MONitor), mainline since Linux 5.15, replaces the scan with sampling over a bounded number of address *regions*, so monitoring cost is a function of the region count rather than the page count. The cost of that bound is resolution: each region is represented by one sampled page per interval, so per-page accuracy is traded for a per-region estimate whose quality depends on how well the adaptive partitioning tracks the workload.

## Region-based sampling

A *region* is a contiguous range of address space whose pages are **assumed to share an access frequency**. Each sampling interval, DAMON picks a single representative page in each region, checks its page-table-entry (PTE) Accessed bit, and clears it; a bit found set means the region was accessed at least once since the previous check.

Two intervals drive the loop:

- The **sampling interval** (default 5 ms) sets how often a region's representative page is probed.
- The **aggregation interval** (default 100 ms) is the window over which per-region access counts (`nr_accesses`) accumulate before being reported and reset.

The defaults therefore stand in a **1:20 ratio**, giving on the order of twenty samples per region per aggregation window. Each region therefore contributes at most one PTE check per sampling interval, and the accumulated `nr_accesses` for an aggregation interval is bounded above by the number of samples taken in it.

The quantity that bounds cost is the **region count, not the page count**. The configuration fixes `min`/`max` region bounds, and the work per interval is proportional to the number of live regions. A 4 KiB process and a 4 TiB process therefore cost the same to monitor if both are partitioned into, say, 10–1000 regions. What differs is the address span each region covers, and hence the strength of the shared-frequency assumption.

## Adaptive regions

A fixed partitioning stops being representative as soon as the access pattern moves, so DAMON continuously **merges and splits** regions:

- Each aggregation interval, regions are **split** at randomly chosen points, so a region whose interior is not uniform gets a chance to be resolved.
- Adjacent regions whose access frequencies are **similar are merged back**, which undoes the splits that revealed nothing.

Over a few aggregation intervals the partitioning converges toward one in which each region contains pages of similar temperature — the assumption the sampling rests on — while the total region count stays inside the configured budget. The two mechanisms pull against each other: splitting spends region budget probing for internal structure, merging reclaims it wherever the probe found none.

## Virtual and physical monitoring

The operations layer is pluggable, and the choice determines what is observable:

- **vaddr** monitors one process's virtual address space, following its mappings.
- **fvaddr** monitors fixed virtual address ranges.
- **paddr** monitors the physical address space directly, using the reverse mapping (rmap) to walk every page table that maps a given address.

Physical monitoring is what makes DAMON applicable to system-wide, cross-process tiering, because it observes a page regardless of which process owns it. Some actions are restricted to one operations set, described below.

## DAMOS: acting on the heatmap

DAMOS (DAMON-based Operation Schemes) attaches an **action** to an **access pattern**. A pattern is a set of minimum/maximum bounds on three axes:

1. region size,
2. `nr_accesses`,
3. **age** — how long the region's access frequency has been kept unchanged, counted in aggregation intervals.

A region to which the action is applied must satisfy **all three** bounds simultaneously. The age axis is what separates a momentarily quiet region from a durably cold one: a region that has only now dipped to zero accesses has a small age and does not match an age-gated scheme.

Available actions include `pageout` (reclaim), `hugepage` / `nohugepage` (advise transparent-hugepage collapse or split, vaddr only), `lru_prio` / `lru_deprio` (move pages toward the hot or cold end of the least-recently-used lists, paddr only), `migrate_hot` / `migrate_cold` for tiered memory, and `stat`, which records what would have been acted on without acting.

Three governors constrain a scheme:

- **Quotas** cap the time or the number of bytes a scheme may touch per reset interval. When the quota binds, DAMOS does not act on an arbitrary subset: it **prioritizes regions by a weighted score over region size, access frequency and age**, with configurable weights, and spends the quota on the highest-scoring regions first. The direction of the score is action-aware, so a `pageout` scheme under quota drains the coldest, oldest regions first.
- **Watermarks**, such as `free_mem_rate`, activate a scheme only while the monitored metric is inside the configured band, keeping it inert outside memory pressure.
- **Filters** exclude classes of pages, for example anonymous pages or young pages, from a scheme that would otherwise match them.

## Driving it

The sysfs interface is rooted at `/sys/kernel/mm/damon/admin/`. Writing to it directly works; the `damo` user-space tool is the practical front end. Recording a physical-address heatmap and viewing it:

```bash
sudo damo record paddr        # writes ./damon.data
sudo damo report heatmap --resol 15 80
sudo damo report wss          # working-set-size distribution
```

Proactive pageout of memory that has been idle for at least 5 seconds, with a per-second byte cap:

```bash
sudo damo start \
    --damos_action pageout \
    --damos_access_rate 0% 0% \
    --damos_age 5s max \
    --damos_quota_interval 1s \
    --damos_quota_space 200MB
```

`--damos_access_rate 0% 0%` is the frequency bound, `--damos_age 5s max` the age bound; both must hold. The quota pair bounds the scheme to 200 MB per 1 s reset interval, so a sudden expansion of the cold set drains at a bounded rate rather than in one burst.

The same scheme expressed in raw sysfs sets `schemes/0/action` to `pageout`, fills `access_pattern/{sz,nr_accesses,age}/{min,max}`, and writes `quotas/ms`, `quotas/bytes` and `quotas/reset_interval_ms`, before committing the kdamond with:

```bash
echo on > /sys/kernel/mm/damon/admin/kdamonds/0/state
```

Parameters written to sysfs are read when the kdamond is started; a value changed under a running kdamond is not applied by the write alone, and requires `commit` to be written to the same `state` file.

Bounded-overhead monitoring combined with quota-limited, age-gated actions is the property that makes DAMON deployable as a continuously running proactive reclaim or tiering engine rather than a diagnostic run under supervision.

## Pitfalls

- **Too few regions over a large address space** flattens the heatmap: one representative page stands in for a very wide range, so a hot sub-range and a cold sub-range merge into a single lukewarm `nr_accesses` value and neither an age-gated `pageout` nor a `lru_prio` scheme matches correctly.
- **A sampling interval close to the aggregation interval** leaves few samples per aggregation window, so `nr_accesses` becomes coarse; the defaults keep the two an order of magnitude apart, at 5 ms and 100 ms.
- **A scheme with no age bound** acts on regions that are momentarily quiet rather than durably cold, since `nr_accesses` can reach zero for one aggregation interval in an actively used region.
- **A scheme with no quota** has no bound on bytes touched per interval, so a workload shift that makes a large span match the pattern is acted on at once.
- **Choosing the wrong operations set** silently disables the action: `hugepage`/`nohugepage` apply to vaddr, `lru_prio`/`lru_deprio` to paddr.
- **vaddr monitoring is scoped to one process**, so cross-process residency and page-cache pages shared between processes are invisible to it; system-wide tiering requires paddr.
- **Skipping a `stat` dry run** deploys an untested pattern: `stat` reports what a scheme would have matched without applying the action, which is what sizing a quota against a real workload requires before reclaim begins.
