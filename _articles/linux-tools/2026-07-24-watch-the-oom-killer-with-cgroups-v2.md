---
title: "Making the OOM killer fire on command with cgroups v2"
date: 2026-07-24
track: linux-tools
summary: "Memory limits stay abstract until the kernel kills a process for crossing one. cgroups v2 reproduces that event deterministically in three commands, and separates the hard wall from the throttle."
reading_time: 6
tags: [cgroups, memory, kernel, systemd]
sources:
  - title: "Kernel docs: Control Group v2"
    url: "https://docs.kernel.org/admin-guide/cgroup-v2.html"
  - title: "systemd.resource-control(5)"
    url: "https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html"
---

**Gist.** "The container was OOM-killed" is repeated far more often than it is observed, so the mechanism behind it — which limit was crossed, which process the kernel picked, and what it recorded afterwards — stays guesswork. The unified control-group hierarchy (cgroups v2) exposes that mechanism as files under `/sys/fs/cgroup`: a memory limit is a decimal number written to `memory.max`, and the kill is counted in `memory.events`. The cost of reproducing it is that the experiment must run inside the limited cgroup, so a careless write of the wrong process identifier (PID) puts the operator's own shell — and every command started from it afterwards — behind the wall.

## The hierarchy and the two limits

cgroups v2 mounts a **single unified hierarchy**, conventionally at `/sys/fs/cgroup`, in place of the per-controller hierarchies of version 1. A cgroup is a directory; creating one with `mkdir` is the whole creation protocol. The controller interface files appear inside it, prefixed by controller name: `memory.max`, `memory.current`, `memory.events`, `memory.stat`, `cpu.stat`.

The memory controller distinguishes two limits, and the distinction is the substance of the topic:

- **`memory.max` is the hard limit.** When a charge to the cgroup cannot be satisfied by reclaim, the kernel invokes the out-of-memory (OOM) killer against a task in that cgroup. The workload does not get to continue past the number.
- **`memory.high` is the throttle.** Crossing it does not kill anything. The kernel drives reclaim on the cgroup and **throttles the allocating tasks**, imposing delay on the processes responsible for the overage so that usage is pushed back under the line. A workload that exceeds `memory.high` runs slowly; a workload that exceeds `memory.max` stops running.

Setting `memory.high` strictly below `memory.max` therefore produces a graded response: reclaim and stall first, kill only if the stall fails to recover the memory. Both files accept the literal string `max` to mean no limit, which is also their default.

## Reproducing the kill by hand

Create a cgroup and cap it at 50 MiB:

```bash
sudo mkdir /sys/fs/cgroup/demo
echo "50M" | sudo tee /sys/fs/cgroup/demo/memory.max
```

Membership is set by writing a PID into `cgroup.procs`. **The file accepts one PID per write**, and migration in the unified hierarchy has process granularity: writing a PID moves that process together with all of its threads. The shell moves itself in so that its children inherit the placement:

```bash
echo $$ | sudo tee /sys/fs/cgroup/demo/cgroup.procs   # move THIS shell in
```

A child of that shell now allocates well past the cap:

```bash
python3 -c "b = bytearray(200*1024*1024); input()"
```

`bytearray` of 200 MiB is zero-filled at construction, so the pages are touched rather than merely reserved, and the charge is real. The process reports `Killed`. The evidence is in two places:

```bash
cat /sys/fs/cgroup/demo/memory.events   # oom_kill 1
dmesg | tail                            # the kernel's OOM report
```

`memory.events` is a **flat keyed file of monotonically increasing counters**, one line per event class. The relevant keys are `high` (times usage exceeded `memory.high` and the cgroup was throttled), `max` (times usage was about to go over the `memory.max` boundary), `oom` (times usage reached the limit and an allocation was about to fail) and `oom_kill` (**number of processes in this cgroup killed by any kind of OOM killer**). Counters never decrease, so a monitoring system reads a delta, not a level. The kernel's report in `dmesg` names the killed process, its resident set size and the cgroup it belonged to.

The experiment is deterministic in a way the production incident is not: the limit is fixed, the allocation is fixed, and no other tenant competes for the same budget.

## Watching the throttle instead of the cliff

Adding a `memory.high` below `memory.max` converts the same allocation into a stall:

```bash
echo "40M" | sudo tee /sys/fs/cgroup/demo/memory.high
```

Two files show the effect while the workload runs. **`memory.current` reports the total memory currently charged to the cgroup and its descendants** — it is the number the limits are compared against. The `high` counter in `memory.events` increments as the cgroup is pushed back under the throttle. A workload that allocates a transient working set above `memory.high` but below `memory.max` will show a rising `high` count with `oom_kill` still at zero: that is back-pressure working.

`memory.stat` breaks the charge down by category (anonymous pages, page cache, kernel structures), which is what distinguishes a genuine leak from page cache that reclaim would have surrendered on demand.

## Delegating the same limits to systemd

In production the files are rarely written directly, because **systemd owns the hierarchy** and writes them on behalf of units. `systemd-run --scope` creates a transient scope unit with the same limits:

```bash
systemd-run --scope -p MemoryMax=50M -p MemoryHigh=40M \
  python3 -c "bytearray(200*1024*1024)"
```

`MemoryMax=` sets `memory.max`; `MemoryHigh=` sets `memory.high`. `systemd.resource-control(5)` documents the correspondence directly, and the same property names are valid in a unit file's `[Service]` section. Live usage is read back with `systemctl show <unit> -p MemoryCurrent`, which surfaces `memory.current` for that unit's cgroup.

The pairing matters for a second reason: because systemd manages the hierarchy, a directory created by hand under `/sys/fs/cgroup` is outside its model of the tree. Delegation — handing a subtree to another manager — is the supported way to own cgroups that systemd will not touch.

Cleaning up the hand-built cgroup requires that it be empty first: move the shell back to another cgroup, then `sudo rmdir /sys/fs/cgroup/demo`. A cgroup directory with live members will not be removed.

The CPU controller tells a structurally identical story for scheduling latency rather than for memory. Adding `-p CPUQuota=20%` to the `systemd-run` invocation caps the unit's CPU bandwidth, and the throttling counters in `cpu.stat` accumulate as the workload is held back — a stall that is recorded rather than a kill that is fatal.

## Pitfalls

- **Writing `$$` into `cgroup.procs` moves the interactive shell itself.** Every subsequent command in that terminal, including the editor or package manager typed next by reflex, is charged against the 50 MiB cap and is a candidate for the kill.
- **`cgroup.procs` takes one PID per write.** Echoing a whitespace-separated list does not migrate the list; the processes stay where they were, and the experiment silently measures the wrong set of tasks.
- **`memory.events` counters are cumulative.** Reading `oom_kill 1` after a second experiment does not mean the second run killed something; only the difference between two reads carries that information.
- **The OOM killer selects a process in the cgroup, not necessarily the allocator.** The `dmesg` report names which one was chosen, and it may be a sibling of the process that requested the memory.
- **`rmdir` fails while the cgroup has members.** The removal is refused rather than forcing the tasks elsewhere, so the shell must be migrated out first.
- **A hand-created directory under `/sys/fs/cgroup` is invisible to systemd's model of the tree.** systemd's rule is that a cgroup has a single writer, so unit properties are the durable path, and delegation is the supported way to hold a subtree outside them.
- **Allocation without touching pages is not a charge.** A reservation that never faults in its pages does not raise `memory.current`, so an experiment that only reserves address space will not trip either limit.
