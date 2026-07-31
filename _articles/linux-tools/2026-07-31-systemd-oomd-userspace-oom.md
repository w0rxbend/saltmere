---
title: "systemd-oomd: kill the runaway before the kernel's OOM killer freezes your box"
date: 2026-07-31
track: linux-tools
summary: "The kernel OOM killer only fires once you are truly out of memory — by which point the machine has spent minutes thrashing swap and is unusable. systemd-oomd watches PSI pressure and cgroup swap usage from userspace and kills a whole cgroup early, while the system is still responsive. Here's how to configure it per-service."
reading_time: 5
tags: [systemd-oomd, psi, cgroups-v2, oom, memory-pressure, linux]
sources:
  - title: "systemd-oomd.service(8) — man page (freedesktop / man7)"
    url: "https://www.man7.org/linux/man-pages/man8/systemd-oomd.8.html"
  - title: "oomd.conf(5) — DefaultMemoryPressureLimit / DurationSec"
    url: "https://www.freedesktop.org/software/systemd/man/latest/oomd.conf.html"
  - title: "systemd.resource-control(5) — ManagedOOMSwap / ManagedOOMMemoryPressure"
    url: "https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html"
  - title: "Fedora — Changes/EnableSystemdOomd"
    url: "https://fedoraproject.org/wiki/Changes/EnableSystemdOomd"
  - title: "Linux kernel docs — Pressure Stall Information (PSI)"
    url: "https://docs.kernel.org/accounting/psi.html"
---

You have watched this happen: a process starts leaking, free memory drains, and the machine begins swapping. Not crashing — *thrashing*. The mouse stutters, SSH takes 40 seconds to echo, and the kernel's OOM killer sits on its hands because, technically, there is still a sliver of swap left. By the time the kernel finally acts, you have lost several minutes of a wedged system. The kernel OOM killer is a **last resort**; it optimizes for "don't kill anything unnecessarily," which is the opposite of what you want on an interactive box.

`systemd-oomd` is the fix: a userspace OOM killer that acts *early*, on the theory that a system spending most of its time reclaiming memory is already broken even if it hasn't run out.

## What it watches: PSI, not free bytes

The insight is to stop measuring free memory and start measuring **stall time**. Pressure Stall Information (PSI), exposed in `/proc/pressure/memory` and per-cgroup at `memory.pressure`, reports the fraction of wall-clock time tasks were *stalled waiting on memory* (reclaim, swap-in, page faults):

```
$ cat /proc/pressure/memory
some avg10=0.00 avg60=0.00 avg300=0.00 total=1234
full avg10=0.00 avg60=0.00 avg300=0.00 total=567
```

`some` is "at least one task stalled"; `full` is "everything stalled." A healthy system reads ~0. When a cgroup's `full avg10` climbs to, say, 60%, that cgroup is spending most of its time waiting on memory — a far better "in trouble" signal than a free-byte threshold, because it captures thrashing that free-memory numbers hide.

systemd-oomd requires **cgroups v2** (the unified hierarchy) and the PSI accounting it provides. It monitors two things per cgroup: sustained **memory pressure** and **swap usage**.

## Turning it on for a specific service

systemd-oomd only supervises cgroups you *opt in*, via two properties in `systemd.resource-control`:

- **`ManagedOOMMemoryPressure=kill`** — watch this cgroup's PSI; if pressure stays above the limit for the configured duration, kill a process in it.
- **`ManagedOOMSwap=kill`** — if system swap usage crosses the global threshold, kill the swap-heaviest managed cgroup.

Say you run a memory-hungry batch job you'd rather sacrifice than let it take down the host. Drop an override:

```ini
# /etc/systemd/system/batch-crunch.service.d/oomd.conf
[Service]
ManagedOOMMemoryPressure=kill
ManagedOOMMemoryPressureLimit=60%   # this service's own PSI threshold
```

```bash
systemctl daemon-reload
systemctl restart batch-crunch.service
# confirm oomd sees it:
systemctl status systemd-oomd
oomctl                    # dumps monitored cgroups + live pressure/swap
```

`oomctl` is the diagnostic you want — it prints every cgroup under management with its current pressure and swap numbers, so you can see *why* oomd would or wouldn't act.

## The system-wide defaults

Global behavior lives in `oomd.conf`:

```ini
# /etc/systemd/oomd.conf  (or a drop-in under oomd.conf.d/)
[OOM]
SwapUsedLimit=90%                       # act when total swap is this full
DefaultMemoryPressureLimit=60%          # default PSI limit for managed cgroups
DefaultMemoryPressureDurationSec=30s    # pressure must persist this long
```

`DefaultMemoryPressureDurationSec` is the anti-flap knob: pressure must stay above the limit *continuously* for that long before oomd fires, so a brief compile spike doesn't get your job killed. This is why Fedora enables systemd-oomd by default — a leaking browser tab gets its cgroup reaped in seconds instead of hanging the desktop.

## The behavior that will surprise you: it kills the whole cgroup

systemd-oomd does **not** hunt for the single worst process. It selects a monitored **cgroup** and kills everything in it (via `cgroup.kill`). The unit of death is the service/scope, not the PID. That is usually what you want — a leaking service and its worker children go together — but it means you should only enable `kill` on cgroups you are genuinely willing to lose *entirely*. Enabling it on `user.slice` can take out an entire login session at once. Scope it to the specific services that misbehave, set a duration long enough to ignore normal spikes, and leave critical services unmanaged so the kernel remains their only (reluctant) killer.

**Try next:** enable `ManagedOOMMemoryPressure=kill` on a throwaway service, then run a memory bomb inside it — `stress-ng --vm 4 --vm-bytes 90% --timeout 120s`. Watch `oomctl` and `journalctl -u systemd-oomd -f`: you'll see pressure climb, hold past the duration, and oomd reap the cgroup while your shell stays perfectly responsive. Compare that to running the same bomb outside any managed cgroup and waiting on the kernel — the responsiveness difference is the entire point.
