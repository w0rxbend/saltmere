---
title: "MGLRU: Multi-Generational Page Reclaim in the Linux Kernel"
date: 2026-07-31
track: linux-tools
summary: "How the classic active/inactive LRU aged pages coarsely and spent CPU on reverse-map scans under pressure, and how MGLRU's generations plus direct page-table Accessed-bit scanning replace it. Merged in 6.1, tunable through sysfs and debugfs."
reading_time: 6
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

**Gist.** The classic Linux page-reclaim algorithm keeps two lists per NUMA node — active and inactive — which encodes page age in a single bit and locates accessed pages by reverse-mapping (rmap), that is, by walking every process that maps a candidate page. The multi-generational LRU (MGLRU) replaces the two lists with a small ladder of **generations** and finds accessed pages by walking process page tables directly, clearing the page-table entry (PTE) Accessed bit in bulk. The cost is a second, parallel reclaim implementation in the kernel with its own tuning surface and its own workload-dependent biases, rather than a drop-in replacement for the old path.

## What the two-list design could not express

Under the classic scheme a page carries a coarse "active or not" status derived from the PTE Accessed bit. Two consequences follow. **Aging accuracy is limited by the width of that state**: a barely-warm page and a continuously hot page occupy the same list, so the reclaimer has no ordering between them and can evict part of a working set while colder pages remain resident.

The second consequence concerns the scan itself. Selecting victims requires answering "was this page accessed?", and with only a physical page in hand the answer comes from rmap: for each candidate, follow the reverse mapping to every address space that maps it and inspect the corresponding PTE. That access pattern is driven by the order of pages on the list, so it **jumps between unrelated processes' page tables**, with poor locality in the CPU caches and translation lookaside buffer. The scan is heaviest precisely when the machine is under memory pressure.

MGLRU was written by Yu Zhao at Google and merged into **Linux 6.1** in October 2022. Google shipped it on Chrome OS and Android before mainlining.

## Generations, `min_seq` and `max_seq`

MGLRU sorts pages into **generations** — buckets ordered by how long it has been since the page was last observed as accessed. By default there are **up to four generations per node**, tracked separately for anonymous and file pages. Two sequence counters delimit the ladder: `max_seq` names the youngest generation, `min_seq` the oldest. The invariant is that every tracked folio belongs to exactly one generation in `[min_seq, max_seq]`, and generation membership only ever moves toward `max_seq` (promotion) or out of the ladder (eviction).

Two operations drive the state machine:

- **Aging** produces generations. It scans for recently accessed pages, promotes them to `max_seq`, and then increments `max_seq` so that a fresh, empty youngest generation exists. Pages not promoted therefore become relatively older without being touched.
- **Eviction** consumes generations. It reclaims folios from `min_seq`; when that generation is empty, `min_seq` advances toward `max_seq`.

The ladder is what supplies the age ordering the two-list scheme lacked: a page's position among four generations is a strictly finer signal than one active/inactive bit.

## Scanning page tables instead of rmap

The larger change is the direction of the scan. Aging walks **process page tables directly**, reading and clearing Accessed bits in PTEs in bulk, instead of starting from a physical page and reverse-mapping back to its users. Walking the page tables in address order gives the scan sequential locality.

A page-table walk is wasteful over sparse address spaces, where most entries map nothing recently used. MGLRU therefore maintains a **Bloom filter** used to skip page-table pages holding few active entries. A Bloom filter answers set membership with **no false negatives and a tunable false-positive rate**, so the effect is that some low-value page-table pages are still walked, never that a promising one is skipped incorrectly.

Refault tracking closes the loop. **Refaults are accounted per generation** — a page evicted from `min_seq` and faulted back in shortly afterwards is recorded against the generation it left — and that signal feeds back into how aggressively each generation is reclaimed. The admin-guide documentation does not spell out the control law, so the exact feedback behaviour has to be read from the source.

## Enabling and inspecting

MGLRU requires `CONFIG_LRU_GEN=y`; `CONFIG_LRU_GEN_ENABLED=y` selects it at boot rather than leaving it off until enabled at runtime. Whether a given distribution kernel sets the second option varies, and the sysfs file below is the authoritative check. The runtime control is a bitmask in sysfs:

```bash
# Non-zero bitmask indicates MGLRU is active
cat /sys/kernel/mm/lru_gen/enabled        # e.g. 0x0007

# Enable all features (equivalent to writing 0x0007)
echo y | sudo tee /sys/kernel/mm/lru_gen/enabled
```

The bits select independent features:

| Bit | Meaning |
|--------|---------|
| `0x0001` | Core multi-gen LRU |
| `0x0002` | Batch-clear Accessed bits in leaf PTEs |
| `0x0004` | Clear Accessed bits in non-leaf (higher-level) PTEs |

Writing `5` therefore enables the core plus non-leaf clearing (`0x0001 | 0x0004`), leaving leaf batch-clearing off.

A thrash guard, `min_ttl_ms`, holds back eviction of the working set of the past *N* milliseconds. It defaults to `0`, which disables the guard. The documented consequence of raising it is that the larger the value, the greater the chance of a premature out-of-memory (OOM) kill: reclaim is withheld while allocations continue to fail. No published figure fixes a safe upper bound, so the value is workload-specific.

```bash
echo 1000 | sudo tee /sys/kernel/mm/lru_gen/min_ttl_ms
```

## The debugfs interface

With debugfs mounted, `/sys/kernel/debug/lru_gen` exposes aging and eviction as explicit commands, which supports working-set estimation and **proactive reclaim**. Commands identify a target by `memcg_id` and `node_id`.

```bash
# Age: + memcg_id node_id max_gen_nr [can_swap [force_scan]]
echo '+ 0 0 0 1 1' | sudo tee /sys/kernel/debug/lru_gen

# Reclaim: - memcg_id node_id min_gen_nr [swappiness [nr_to_reclaim]]
echo '- 0 0 0 200 4096' | sudo tee /sys/kernel/debug/lru_gen
```

Built with `CONFIG_LRU_GEN_STATS=y`, `/sys/kernel/debug/lru_gen_full` reports per-generation timestamps and page counts, which is the direct way to check that aging tracks a given workload rather than inferring it from aggregate reclaim counters.

The efficiency of a reclaim configuration is observable without debugfs as well: `/proc/vmstat` exposes `pgscan_kswapd` and `pgsteal_kswapd`, and their ratio is pages scanned per page reclaimed. Comparing that ratio with the bitmask set to `0` and to `0x0007` under the same load gives a like-for-like measure of scan efficiency between the two reclaim paths.

```bash
grep -E '^pg(scan|steal)_kswapd' /proc/vmstat
```

## Status

MGLRU is stable and widely deployed but remains an **alternative** to the classic LRU rather than the sole mechanism; both code paths coexist. LWN's March 2026 write-up "Reconsidering the multi-generational LRU" records the outstanding complaints: reclaim is not balanced between anonymous and file-backed pages, with anonymous pages staying in the younger generations; readahead pages are placed in the youngest generation regardless of whether they are subsequently used; some workloads regress; MGLRU's metrics diverge from those of the classic LRU, which complicates consumers such as Android's OOM daemon; it consumes three page flags; and it is expensive on low-end devices with few reclaimable pages. Maintenance thinned after Google stepped back. Axel Rasmussen, a listed maintainer, said in that discussion that work to correct this would begin in April. The discussion did not settle on making MGLRU the default or removing the classic LRU.

## Pitfalls

- Setting `min_ttl_ms` high withholds eviction of the recent working set for that long, which can turn memory pressure into a premature OOM kill instead of reclaim.
- Reading `/sys/kernel/mm/lru_gen/enabled` and seeing a non-zero value does not mean all features are on: the value is a bitmask, and `1` means the core only, with both Accessed-bit clearing modes disabled.
- Writing a decimal number to `enabled` sets the whole mask, not a single bit; `echo 4` disables the core (`0x0001`) as a side effect of enabling non-leaf clearing.
- `/sys/kernel/debug/lru_gen_full` is absent unless the kernel was built with `CONFIG_LRU_GEN_STATS=y`, so a missing file indicates a build option rather than a disabled MGLRU.
- Anonymous pages staying in the younger generations while file pages are evicted shows up as high file-page refault rates under pressure; this imbalance is a reported, unresolved MGLRU issue rather than a misconfiguration.
- MGLRU's reclaim counters do not line up with the classic LRU's, so tooling calibrated against the old path — Android's OOM daemon is the reported case — can misread pressure after the switch.
- The debugfs aging and eviction commands act on one `memcg_id`/`node_id` pair, so a command aimed at memcg 0 leaves other cgroups untouched and can appear to have no effect on a workload confined to a different cgroup.
