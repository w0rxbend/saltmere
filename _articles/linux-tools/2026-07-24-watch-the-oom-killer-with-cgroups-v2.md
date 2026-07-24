---
title: "Make the OOM killer fire on command with cgroups v2"
date: 2026-07-24
track: linux-tools
summary: "You don't really understand memory limits until you've watched the kernel kill a process for crossing one. cgroups v2 lets you set that up in three commands."
reading_time: 4
tags: [cgroups, memory, kernel, systemd]
sources:
  - title: "Kernel docs: Control Group v2"
    url: "https://docs.kernel.org/admin-guide/cgroup-v2.html"
  - title: "systemd.resource-control(5)"
    url: "https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html"
---

"Container got OOM-killed" is one of those phrases people repeat without having seen it happen. Ten minutes with cgroups v2 fixes that. Every modern distro mounts the unified hierarchy at `/sys/fs/cgroup`, and you can drive it directly — no Docker required.

## Build a memory jail by hand

Create a cgroup, cap it at 50 MiB, and put your shell in it:

```bash
sudo mkdir /sys/fs/cgroup/demo
echo "50M" | sudo tee /sys/fs/cgroup/demo/memory.max
echo $$   | sudo tee /sys/fs/cgroup/demo/cgroup.procs   # move THIS shell in
```

Now allocate more than the cap from inside that same shell:

```bash
python3 -c "b = bytearray(200*1024*1024); input()"
```

The process gets `Killed`. Check why:

```bash
cat /sys/fs/cgroup/demo/memory.events   # oom_kill 1
dmesg | tail                            # the kernel's OOM report
```

You just reproduced, deterministically, the thing that pages people at 3am.

## The knob most people miss: memory.high vs memory.max

`memory.max` is the hard wall — cross it and you're killed. `memory.high` is a *throttle*: the kernel reclaims aggressively and stalls the process to keep it under the line, but doesn't kill it. Set `memory.high` below `memory.max` and you get graceful back-pressure instead of a cliff:

```bash
echo "40M" | sudo tee /sys/fs/cgroup/demo/memory.high
```

Watch `memory.current` and the `high` counter in `memory.events` as the process fights to stay under the throttle. This single distinction is why a well-configured service degrades instead of crashing under memory pressure.

## The 30-second version with systemd

You rarely poke `/sys/fs/cgroup` in production — systemd does it for you. The same limit, transiently:

```bash
systemd-run --scope -p MemoryMax=50M -p MemoryHigh=40M \
  python3 -c "bytearray(200*1024*1024)"
```

`systemctl show <unit> -p MemoryCurrent` reads the live usage back out. Every `MemoryMax=` in a unit file is just writing `memory.max` under the hood — now you know exactly what it does.

**Clean up:** `sudo rmdir /sys/fs/cgroup/demo` (after moving your shell back out). **Try next:** add `-p CPUQuota=20%` and watch `cpu.stat`'s throttling counters climb — the CPU controller tells the same story for scheduling latency.
