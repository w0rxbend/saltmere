---
title: "sched_ext: CPU schedulers as BPF programs"
date: 2026-07-30
track: linux-tools
summary: "Merged in Linux 6.12, sched_ext allows a CPU scheduling policy to be implemented as a loadable BPF program, swapped at runtime, and reverted to the default scheduler by a kernel watchdog whenever the policy misbehaves. This article covers the framework, the callbacks that constitute a scheduler, the dispatch-queue model, and the loading procedure."
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

**Gist.** Altering how Linux selects the next task to run has historically required patching the in-kernel scheduler in C, rebuilding, and rebooting, so the feedback loop was bounded by kernel release cadence. `sched_ext` — the extensible scheduler class merged in **Linux 6.12** (released 17 November 2024, a long-term-support kernel) — permits a scheduling policy to be expressed as a **Berkeley Packet Filter (BPF)** program that is loaded into a running kernel and assumes CPU scheduling immediately. The cost is that the policy is never trusted: the program is checked by the BPF verifier before it loads and watched by a runtime stall detector once it runs, and any detected fault discards the policy and restores the default scheduler mid-flight.

## The abort path is the load-bearing mechanism

A CPU scheduler is among the most dangerous components to delegate to an external program: a policy that never selects some runnable task wedges the system, and no amount of static checking can prove liveness of an arbitrary program. The kernel documentation states the guarantee directly:

> The system integrity is maintained no matter what the BPF scheduler does. The default scheduling behavior is restored anytime an error is detected, a runnable task stalls, or on invoking the SysRq key sequence SysRq-S.

Three independent triggers therefore exist. **The BPF verifier** rejects, before load, a program that could crash the kernel — memory safety and termination of individual callbacks are established statically. **The runtime watchdog** covers what the verifier cannot: it observes runnable tasks that receive no CPU time, and aborts the scheduler when such a stall appears. **`SysRq-S`** is the manual override, available even when the policy is starving the process that would otherwise be used to unload it.

The invariant is that on any of these triggers every task is migrated back to the fair-class scheduler (EEVDF/CFS) and execution continues. The observable failure mode is the experiment terminating with a logged reason, not an unresponsive machine.

## The scheduler as a set of callbacks

A sched_ext scheduler is a BPF `struct_ops` instance populating callbacks in `struct sched_ext_ops`. The set need not be complete; the kernel supplies defaults for callbacks left unimplemented. Three constitute the hot path:

| Callback | When it fires | Responsibility |
|---|---|---|
| `select_cpu()` | A task wakes | Choose a target CPU (an optimisation hint); optionally wake an idle CPU |
| `enqueue()` | A task becomes runnable | Insert into a dispatch queue (DSQ), or retain it internally |
| `dispatch()` | A CPU exhausts its work | Move tasks from DSQs onto that CPU's local queue |

The unit of bookkeeping is the **dispatch queue (DSQ)**. A built-in global DSQ exists, one local DSQ exists per CPU, and a scheduler may create further DSQs of its own. **`enqueue` determines where a runnable task waits; `dispatch` determines what an idle CPU pulls next.** A substantial range of policy — priority tiers, per-workload isolation, latency boosting for interactive tasks — reduces to the choice of destination DSQ and the drain order over DSQs.

The result returned by `select_cpu()` is a hint rather than a binding placement, so a policy cannot express affinity by that callback alone; placement is settled when a task is dispatched onto a CPU's local queue.

Lifecycle callbacks `init()` and `exit()` bracket the run. **`exit()` receives the reason the scheduler is stopping**, which is where the cause of a watchdog abort surfaces: a runnable task stall, a runtime error, or an explicit SysRq.

Because the policy is BPF, it can read BPF maps written from user space. Several production schedulers are split accordingly: a fast BPF component making per-task decisions, plus a userspace daemon computing heavier quantities (load balancing, topology) and pushing them down through maps. `scx_rusty` has this shape.

## Existing schedulers

Two directions are represented in the [sched-ext/scx](https://github.com/sched-ext/scx) tree:

- **Latency-oriented.** `scx_lavd` (Latency-criticality Aware Virtual Deadline) targets interactivity and includes "core compaction", packing work onto fewer cores when utilisation is low. `scx_bpfland` is a vruntime-based scheduler prioritising interactive tasks with cache-aware CPU selection. CachyOS packages both.
- **Throughput-oriented.** `scx_rusty` is a general-purpose, tunable multi-domain scheduler; `scx_layered` partitions tasks into configurable "layers" carrying different policies. LWN's account of [SCHED_EXT and IOCost](https://lwn.net/Articles/966618/) documents Meta's use of sched_ext against production workloads.

The property common to both is iteration speed: two policies can be compared on the same running kernel by stopping one process and starting another.

## Loading procedure

**1. Confirm kernel support.** `CONFIG_SCHED_CLASS_EXT=y` is required, together with the BPF stack (`CONFIG_BPF_SYSCALL`, `CONFIG_BPF_JIT`, `CONFIG_DEBUG_INFO_BTF`):

```bash
# whichever the distribution provides
zcat /proc/config.gz | grep SCHED_CLASS_EXT
grep CONFIG_SCHED_CLASS_EXT /boot/config-$(uname -r)
```

When enabled, the runtime interface appears under sysfs. `state` reads `disabled` while nothing is loaded; `ops` names the active scheduler once one is running:

```bash
cat /sys/kernel/sched_ext/state          # -> disabled
cat /sys/kernel/sched_ext/root/ops       # (populated when a scheduler is running)
```

A **6.12 or later kernel** is required. Arch and CachyOS package the schedulers directly; Fedora obtains them from a community repository:

```bash
# Arch / CachyOS
sudo pacman -S scx-scheds scx-tools

# Fedora (CachyOS COPR)
sudo dnf copr enable bieszczaders/kernel-cachyos-addons
sudo dnf install scx-scheds
```

**2. Load a scheduler.** Each scheduler is a standalone binary that loads its BPF program and remains in the foreground until stopped:

```bash
sudo scx_simple      # global-vtime example, minimal smoke test
sudo scx_rusty       # general-purpose scheduler
```

While it runs, `/sys/kernel/sched_ext/state` reads `enabled` and `root/ops` reports the name. **Ctrl-C unloads cleanly, returning every task to the default scheduler**, which is the same end state the watchdog produces on failure.

**3. Exercise the abort path.** Load a scheduler, then invoke the manual kill switch:

```bash
echo s | sudo tee /proc/sysrq-trigger    # SysRq-S: abort the BPF scheduler
```

`state` returns to `disabled`, the scheduler process exits, and `dmesg` records the abort together with its reason (`runnable task stall`, a runtime error, or the SysRq described above). That reason string is the same one a defective scheduler's `exit()` callback receives.

For operating schedulers as a service — automatic start at boot, switching policies — the `scx_loader` daemon and its `scxctl` client wrap the above, for example `scxctl start --sched rusty` and `scxctl stop`.

A useful next step is loading `scx_simple`, confirming `/sys/kernel/sched_ext/root/ops` names it, then reading `scheds/c/scx_simple.bpf.c` in the scx tree: it implements `select_cpu`/`enqueue`/`dispatch` against a single global DSQ, which is the smallest complete model of the framework.

## Pitfalls

- **Treating `select_cpu()` as placement.** The callback returns a hint; the task can still be dispatched elsewhere, so a policy that encodes affinity only there observes tasks running on CPUs it did not choose.
- **Omitting `dispatch()` work for a DSQ.** A task enqueued onto a custom DSQ that no `dispatch()` path ever drains is runnable and never scheduled; the symptom is the watchdog aborting with a runnable task stall rather than a visible hang.
- **Assuming verifier acceptance implies a working policy.** The verifier establishes memory safety and termination of individual callbacks, not that every runnable task eventually runs; liveness bugs load successfully and are caught only at runtime.
- **Running on a kernel below 6.12.** Without `CONFIG_SCHED_CLASS_EXT`, `/sys/kernel/sched_ext` is absent and the scheduler binary fails at load rather than falling back.
- **Interpreting a clean Ctrl-C as evidence of correctness.** Orderly unload and watchdog abort leave the system in the same state, so a successful exit does not distinguish a policy that scheduled every task from one that was torn down.
- **Losing the abort reason.** The cause is delivered to `exit()` and recorded in `dmesg`; once the scheduler process has exited, the console output alone does not indicate whether the stop was requested or forced.
