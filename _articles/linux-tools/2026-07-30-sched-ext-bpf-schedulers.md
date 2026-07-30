---
title: "sched_ext: writing CPU schedulers as BPF programs"
date: 2026-07-30
track: linux-tools
summary: "Merged in Linux 6.12, sched_ext lets you implement a CPU scheduler as a loadable BPF program, hot-swap it at runtime, and rely on a kernel watchdog to revert to the default scheduler the moment your policy misbehaves. Here's what the framework is, the callbacks that make up a scheduler, and how to load a real one in about five minutes."
reading_time: 6
tags: [sched-ext, scx, bpf, ebpf, cpu-scheduler, linux-tools]
sources:
  - title: "Extensible Scheduler Class — The Linux Kernel documentation"
    url: "https://docs.kernel.org/scheduler/sched-ext.html"
  - title: "Linux 6.12 Released With Real-Time Capabilities, Sched_Ext, More — Phoronix"
    url: "https://www.phoronix.com/news/Linux-6.12-Released"
  - title: "sched-ext/scx: sched_ext schedulers and tools (GitHub)"
    url: "https://github.com/sched-ext/scx"
  - title: "Improving performance with SCHED_EXT and IOCost [LWN.net]"
    url: "https://lwn.net/Articles/966618/"
  - title: "sched-ext Tutorial — CachyOS Wiki"
    url: "https://wiki.cachyos.org/configuration/sched-ext/"
---

For decades, changing how Linux picks the next task to run meant patching the kernel scheduler in C, rebuilding, rebooting, and hoping you didn't deadlock the machine. The feedback loop was measured in kernel releases. `sched_ext` — the extensible scheduler class merged in **Linux 6.12** (released 17 November 2024, an LTS kernel) — collapses that loop to seconds. You write a scheduling policy as a **BPF program**, load it into a running kernel, and it takes over CPU scheduling immediately. Get it wrong and a watchdog rips it out and hands control back to the default scheduler before your box locks up.

That safety net is the whole reason this is usable in practice, so start there.

## The watchdog is the point

A CPU scheduler is about the most dangerous thing you can hand to an untrusted program. Forget to ever run a task and the system wedges. sched_ext's answer is that the BPF scheduler is never trusted to be correct. From the kernel documentation:

> The system integrity is maintained no matter what the BPF scheduler does. The default scheduling behavior is restored anytime an error is detected, a runnable task stalls, or on invoking the SysRq key sequence SysRq-S.

Three independent triggers, then: the BPF verifier rejects a program that could crash the kernel before it ever loads; a runtime **watchdog** watches for a runnable task that never gets CPU time (a stall) and aborts the scheduler if one appears; and you always have `SysRq-S` as a manual kill switch. On abort, every task is moved back to the fair-class scheduler (EEVDF/CFS) and the machine keeps running. This is what makes it reasonable to test an experimental scheduler on a laptop you care about — the failure mode is "the experiment stops," not "hold the power button."

## What a scheduler actually is here

A sched_ext scheduler is a BPF struct_ops that fills in a set of callbacks in `struct sched_ext_ops`. You don't have to implement all of them; the kernel supplies defaults. The core of the hot path is three:

| Callback | When it fires | What you do |
|---|---|---|
| `select_cpu()` | A task wakes up | Pick a target CPU (an optimization hint); optionally wake an idle CPU |
| `enqueue()` | Task becomes runnable | Insert it into a dispatch queue (DSQ), or hold it internally |
| `dispatch()` | A CPU runs out of work | Move tasks from your DSQs onto that CPU's local queue |

The unit of bookkeeping is the **dispatch queue (DSQ)**. There's a built-in global DSQ and one local DSQ per CPU, and you can create your own. `enqueue` decides where a runnable task waits; `dispatch` decides what a hungry CPU pulls next. A surprising amount of useful policy — priority tiers, per-workload isolation, latency boosting for interactive tasks — is just "which DSQ does this task land in, and in what order do I drain them." Lifecycle callbacks like `init()` and `exit()` bracket the run; `exit()` receives the reason the scheduler is stopping, which is where you find out *why* the watchdog fired.

Because the policy is BPF, it can read BPF maps populated from user space, so many real schedulers are split: a fast BPF component making per-task decisions plus a userspace daemon computing heavier things (load balancing, topology) and pushing them down through maps. `scx_rusty` is exactly this shape.

## Why anyone bothers

Two audiences, pulling in opposite directions:

- **Latency / desktop / gaming.** `scx_lavd` (Latency-Aware Virtual Deadline) targets interactivity — it minimizes latency spikes and includes "core compaction" that packs work onto fewer cores at higher frequency when utilization is low, for power. `scx_bpfland` is a vruntime-based scheduler that prioritizes interactive tasks with cache-aware CPU selection. Distros like CachyOS ship these to make desktops feel snappier under load.
- **Throughput / servers.** `scx_rusty` is a general-purpose, tunable multi-domain scheduler; `scx_layered` lets you carve tasks into configurable "layers" with different policies — the kind of thing you tune per fleet. LWN's write-up on [SCHED_EXT and IOCost](https://lwn.net/Articles/966618/) documents Meta using it to claw back production performance without shipping a custom kernel.

The common thread is iteration speed: you can A/B two scheduling policies on the same running kernel in the time it takes to Ctrl-C one and start the other. That was previously impossible.

## Trying it

**1. Confirm the kernel supports it.** You need `CONFIG_SCHED_CLASS_EXT=y` (plus the usual BPF stack: `CONFIG_BPF_SYSCALL`, `CONFIG_BPF_JIT`, `CONFIG_DEBUG_INFO_BTF`):

```bash
# whichever your distro provides
zcat /proc/config.gz | grep SCHED_CLASS_EXT
grep CONFIG_SCHED_CLASS_EXT /boot/config-$(uname -r)
```

If it's enabled, the runtime interface exists under sysfs. `state` reads `disabled` when nothing is loaded, and `ops` names the active scheduler once one is:

```bash
cat /sys/kernel/sched_ext/state          # -> disabled
cat /sys/kernel/sched_ext/root/ops       # (populated when a scheduler is running)
```

You need a **6.12+ kernel**. On CachyOS/Arch the schedulers are packaged directly; Fedora and Ubuntu pull them from community/enablement repos:

```bash
# Arch / CachyOS
sudo pacman -S scx-scheds scx-tools

# Fedora (CachyOS COPR)
sudo dnf copr enable bieszczaders/kernel-cachyos-addons
sudo dnf install scx-scheds
```

**2. Load a scheduler.** Each scheduler is a standalone binary that loads its BPF program and stays in the foreground; it runs until you stop it. Start simple, then try a real one:

```bash
sudo scx_simple      # trivial global-vtime example — good first smoke test
sudo scx_rusty       # general-purpose production-grade scheduler
```

While it runs, `/sys/kernel/sched_ext/state` flips to `enabled` and `root/ops` shows the name. Press **Ctrl-C** and it unloads cleanly, reverting every task to the default scheduler. That clean unload is the same code path the watchdog uses on failure — so "stop it" and "it crashed" land you in the same safe place.

**3. Watch the watchdog do its job.** You don't have to trust the docs on this. Load a scheduler, then trigger the manual kill switch:

```bash
echo s | sudo tee /proc/sysrq-trigger    # SysRq-S: abort the BPF scheduler
```

`state` drops back to `disabled`, the scheduler process exits, and `dmesg` records the abort with the reason (`runnable task stall`, a verifier/runtime error, or the SysRq you just sent). That reason string is exactly what a buggy scheduler's `exit()` callback would surface — the difference between debugging a scheduler and debugging a hang is that here you get a log line instead of a dead machine.

For managing schedulers as a service (auto-start at boot, switch modes), the `scx_loader` daemon and `scxctl` client wrap all of this — e.g. `scxctl start --sched rusty` / `scxctl stop`.

**Try next:** load `scx_simple`, confirm `/sys/kernel/sched_ext/root/ops` shows it, then clone [sched-ext/scx](https://github.com/sched-ext/scx) and open `scheds/c/scx_simple.bpf.c` — it's under ~200 lines and implements `select_cpu`/`enqueue`/`dispatch` against a single global DSQ, which is the smallest complete mental model of the framework you can hold in your head before reaching for `scx_rusty`.
