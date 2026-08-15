---
title: "systemd-oomd: userspace out-of-memory killing driven by pressure stall information"
date: 2026-07-31
track: linux-tools
summary: "The kernel out-of-memory killer fires only once allocation genuinely fails, after the machine has spent minutes thrashing swap. systemd-oomd monitors pressure stall information and cgroup swap usage from userspace and kills an entire managed cgroup earlier, while the system is still responsive. This article covers the monitored signals, the per-service opt-in properties, and the cost of cgroup-granular killing."
reading_time: 6
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

**Gist.** A leaking process can drive a Linux machine into sustained swap thrashing long before any allocation fails, and the kernel's out-of-memory (OOM) killer — a last-resort mechanism that acts only when reclaim can no longer satisfy a request — leaves the system unusable in the interim. `systemd-oomd` is a userspace daemon that instead watches **pressure stall information (PSI)** and cgroup swap usage, and terminates a monitored control group (cgroup) once pressure has stayed above a configured limit for a configured duration. The cost is granularity and certainty: the unit of death is the whole cgroup rather than the worst single process, and the kill happens on a heuristic threshold that may not correspond to any real allocation failure.

## The signal: stall time rather than free bytes

Free-memory counters describe how much memory remains unused, not how much work the system is losing to reclaim. A machine with a few hundred megabytes of swap left is, by that measure, healthy, while in fact spending most of its wall-clock time faulting pages back in.

PSI, documented in the kernel's accounting documentation and exposed at `/proc/pressure/memory` system-wide and at `memory.pressure` per cgroup, reports **the fraction of wall-clock time tasks spent stalled waiting on memory** — reclaim, swap-in, and page faults:

```
$ cat /proc/pressure/memory
some avg10=0.00 avg60=0.00 avg300=0.00 total=1234
full avg10=0.00 avg60=0.00 avg300=0.00 total=567
```

The two lines carry different meanings. **`some` is the fraction of time at least one task was stalled; `full` is the fraction of time all non-idle tasks were stalled simultaneously**, that is, the interval during which no useful work happened at all. Each line gives running averages over 10, 60 and 300 seconds plus a monotonic microsecond total. An idle or comfortable system reads near zero on all of them.

The practical consequence is that a cgroup whose `full avg10` sits at 60% is spending the majority of a ten-second window doing nothing but waiting on memory. That is a state a free-byte threshold cannot express, because the same free-byte reading occurs on a system that is merely full and on a system that is thrashing.

`systemd-oomd` requires **cgroups v2**, the unified hierarchy, since that is where per-cgroup PSI and the `cgroup.kill` interface live. Within that hierarchy it monitors two independent signals per managed cgroup: **sustained memory pressure** and **swap usage**.

## Opt-in is per cgroup

The daemon does not supervise the whole hierarchy. A cgroup is monitored only when its unit opts in through one of the two properties documented in `systemd.resource-control(5)`:

- **`ManagedOOMMemoryPressure=kill`** — the unit's cgroup and its descendant cgroups become candidates. A candidate becomes eligible once its own PSI stays above the effective limit for the effective duration, and the daemon kills the eligible candidate with the most memory pressure.
- **`ManagedOOMSwap=kill`** — the unit's cgroup and its descendants become candidates. Once system swap usage crosses the global threshold, the daemon kills the candidate using the most swap.

Both default to `auto`, under which the daemon does not act on the cgroup's own data. Enabling pressure monitoring for a single sacrificial batch service takes a drop-in override:

```ini
# /etc/systemd/system/batch-crunch.service.d/oomd.conf
[Service]
ManagedOOMMemoryPressure=kill
ManagedOOMMemoryPressureLimit=60%   # this unit's own PSI threshold
```

```bash
systemctl daemon-reload
systemctl restart batch-crunch.service
systemctl status systemd-oomd
oomctl                    # monitored cgroups with live pressure and swap figures
```

`oomctl` is the primary diagnostic: it lists every cgroup currently under management together with its measured pressure and swap usage, which is the only direct way to confirm that an opt-in took effect and to see how far a cgroup is from its threshold.

## System-wide thresholds

Defaults for every managed cgroup come from `oomd.conf(5)`:

```ini
# /etc/systemd/oomd.conf  (or a drop-in under oomd.conf.d/)
[OOM]
SwapUsedLimit=90%                       # act when total swap is this full
DefaultMemoryPressureLimit=60%          # default PSI limit for managed cgroups
DefaultMemoryPressureDurationSec=30s    # pressure must persist this long
```

`DefaultMemoryPressureDurationSec` supplies the hysteresis. **Pressure must exceed the limit continuously for the whole duration before the daemon acts**, so a transient spike — a link step, a compile, a large file copy — does not by itself trigger a kill. A short duration converts normal load peaks into kills; a long one lets a genuinely wedged cgroup stall the machine for that long before anything happens. The two failure directions are symmetric and there is no setting that avoids both.

The swap path is governed by a separate, system-wide condition: `SwapUsedLimit` compares total swap usage against a percentage of total swap, and only once that is exceeded does the daemon select among cgroups with `ManagedOOMSwap=kill`. A cgroup that opts into swap monitoring alone is therefore never killed on pressure, and vice versa.

Fedora enables `systemd-oomd` by default, per the `Changes/EnableSystemdOomd` change proposal.

## The kill is cgroup-granular

`systemd-oomd` does **not** search for a single worst-offending process. It selects a monitored cgroup and terminates its members through **`cgroup.kill`**, the cgroups-v2 interface that kills every process in the cgroup. The unit of death is the service or scope, not the process identifier (PID).

This matches the common case — a leaking service and the workers it forked share a fate, and killing the parent alone would leave orphans holding the memory — but it sets a hard constraint on where `kill` may be enabled. **Every cgroup that can be selected must be one that is acceptable to lose in its entirety.** Enabling the property on a broad slice such as `user.slice` makes the slice and all of its descendants candidates, so the cgroup finally selected may be a whole login session rather than the one process responsible for the pressure.

Two consequences follow for configuration. First, opt-in belongs on the specific units that are known to misbehave and are individually disposable, not on the enclosing slice. Second, critical units are best left at the default `auto`, which leaves the kernel OOM killer as their only reaper — later and less predictable, but conditioned on an actual allocation failure rather than on a pressure heuristic.

## Observing the mechanism

A controlled demonstration uses a throwaway unit with `ManagedOOMMemoryPressure=kill` and a memory load inside it:

```bash
stress-ng --vm 4 --vm-bytes 90% --timeout 120s
```

`oomctl` shows the cgroup's pressure figure rising; `journalctl -u systemd-oomd -f` records the decision when pressure has held past the duration. The comparison that matters is the same load run outside any managed cgroup, where the kernel killer is the only mechanism and acts only once allocation fails.

## Pitfalls

- **A selected session cgroup loses its graphical or SSH session at once**, because `cgroup.kill` applies to every process under the selected cgroup, including the session leader and terminals.
- **A short `DefaultMemoryPressureDurationSec` turns ordinary load peaks into kills**: a compile or a large copy can hold `full` PSI above 60% for several seconds without the system being in trouble.
- **`ManagedOOMSwap=kill` does nothing on a machine with no swap configured**, since the trigger is a percentage of total swap usage.
- **Setting the properties without `systemctl daemon-reload` and a restart of the unit leaves the cgroup unmonitored**; `oomctl` will not list it, and the absence is silent.
- **Enabling `kill` on a unit with `Restart=always` produces a kill-restart loop** if the workload reaches the same pressure each time, since the daemon reacts to pressure and not to repetition.
- **Pressure monitoring requires cgroups v2**; on a system booted into the legacy hierarchy the per-cgroup `memory.pressure` files and `cgroup.kill` are absent and the daemon has nothing to read.
