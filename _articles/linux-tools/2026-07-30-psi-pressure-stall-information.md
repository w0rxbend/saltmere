---
title: "PSI: measuring the time lost to resource contention"
date: 2026-07-30
summary: "Load average cannot separate 'busy' from 'stalled waiting for memory or disk'. Pressure Stall Information reports the fraction of wall-clock time tasks spent blocked on CPU, memory or I/O — how to read /proc/pressure, scope it per cgroup, and register a poll trigger that fires before the OOM killer does."
track: linux-tools
reading_time: 6
tags: [psi, linux, cgroups-v2, memory-pressure, observability, oomd]
sources:
  - title: "PSI - Pressure Stall Information — The Linux Kernel documentation"
    url: "https://docs.kernel.org/accounting/psi.html"
  - title: "psi: pressure stall information for CPU, memory, and IO — LWN.net"
    url: "https://lwn.net/Articles/759658/"
  - title: "Getting Started with PSI — facebookmicrosites.github.io/psi"
    url: "https://facebookmicrosites.github.io/psi/docs/overview"
  - title: "Understand Pressure Stall Information (PSI) Metrics — Kubernetes documentation"
    url: "https://kubernetes.io/docs/reference/instrumentation/understand-psi-metrics/"
---

**Gist.** Load average counts runnable and uninterruptibly sleeping tasks, so a
load of 8 on an 8-core host is equally consistent with full utilisation and with
total wedging on disk; the number cannot distinguish work from waiting.
Pressure Stall Information (PSI), merged in Linux 4.20 and the signal underneath
Meta's `oomd` and `systemd-oomd`, instead accumulates **the wall-clock time
during which tasks were unable to proceed because a resource was unavailable**,
reported separately for CPU, memory and I/O. The cost is that the kernel must
track per-task stall states and maintain running averages continuously, and that
the resulting figures describe lost time rather than naming the process or the
allocation responsible for it.

## The two aggregation states

PSI exposes one file per resource under `/proc/pressure/`:

```
$ cat /proc/pressure/memory
some avg10=0.00 avg60=0.12 avg300=0.05 total=8419283
full avg10=0.00 avg60=0.08 avg300=0.03 total=4210394
```

The two lines are different aggregations over the same underlying stall
accounting, and the distinction carries the entire interpretation:

- **`some`** — the fraction of time during which *at least one* task was stalled
  on the resource. This is a **latency signal**: work was delayed, but other
  work may have proceeded in parallel.
- **`full`** — the fraction of time during which *every* non-idle task was
  stalled simultaneously, so no useful work advanced at all. This is a
  **lost-throughput signal**: the scope in question was effectively frozen on
  that resource.

`avg10`, `avg60` and `avg300` are percentages over trailing 10-, 60- and
300-second windows; `total` is a **cumulative count of microseconds of stall**
since boot (or since cgroup creation), and is the field to use for exact
differencing between two scrapes, because the decaying averages are unsuitable
for summation.

The reading is quantitative in a way load average is not. `memory full
avg60=20.00` states that over the preceding minute, for 20% of the time, the
whole workload was stalled on memory reclaim rather than executing — that is
**12 seconds of the last 60 lost**, not a proxy requiring interpretation.

The CPU file is the exception. If the CPU is the contended resource, some task
is by construction executing on it, so a whole-system `full` figure for CPU does
not describe a reachable state; the kernel documentation covers the `some`
line as the meaningful CPU pressure signal.

## Per-cgroup attribution

PSI is accounted **per cgroup v2** as well as globally. Each cgroup carries its
own `cpu.pressure`, `memory.pressure` and `io.pressure`:

```
$ cat /sys/fs/cgroup/system.slice/my-app.service/memory.pressure
some avg10=4.21 avg60=2.90 avg300=1.10 total=99182734
full avg10=1.80 avg60=1.02 avg300=0.44 total=41028734
```

This converts an aggregate host symptom into an attributed one. A cgroup that
hits its own `memory.max` and thrashes reclaim registers pressure in its own
file while the host totals may stay low, because the stall is confined to the
tasks inside that cgroup. Kubernetes surfaces the same per-cgroup files as
node- and pod-level PSI metrics behind a feature gate, which is what makes "is this container the one
thrashing" answerable without inference from host-wide counters.

The converse also holds and is the more common diagnostic trap: **a cgroup can
show high pressure caused entirely by contention outside it**. I/O pressure in a
container sharing a device with a noisy neighbour is real stall time for that
container's tasks, but the responsible allocation lives elsewhere. PSI localises
the *victim*, not the *cause*.

## Threshold triggers instead of polling

Reading the files in a loop suffices for inspection, but PSI additionally
supports **kernel-side thresholds**: a process writes a trigger specification to
the pressure file and then waits on the file descriptor with `poll()`, receiving
`POLLPRI` when the configured stall budget is exceeded within the configured
window.

```c
// Wake when 'some' memory stall exceeds 150 ms within any 1 s window.
int fd = open("/proc/pressure/memory", O_RDWR | O_NONBLOCK);
const char *trig = "some 150000 1000000";   // stall_us window_us
write(fd, trig, strlen(trig));

struct pollfd pfd = { .fd = fd, .events = POLLPRI };
while (poll(&pfd, 1, -1) > 0) {
    // Threshold crossed: shed load, release caches, or terminate a chosen
    // cgroup — before the kernel OOM killer selects a victim itself.
    handle_memory_pressure();
}
```

The specification has three fields: the aggregation state (`some` or `full`),
the stall budget in microseconds, and the tracking window in microseconds. The
trigger's lifetime is **bound to the open file descriptor** — closing the
descriptor removes the trigger, and no separate deregistration step exists.

The operational value is the ordering relative to the kernel out-of-memory (OOM)
killer. The OOM killer runs when allocation has **already** failed, and selects
its victim by its own heuristic. A PSI trigger fires while reclaim is still
merely expensive, which is what lets userspace act on its own policy: `oomd` and
`systemd-oomd` both watch PSI and terminate a chosen cgroup on their own
criteria rather than deferring to the kernel's choice.

## Where the signal is decisive

The characteristic case is a host that responds slowly while CPU utilisation
looks unremarkable. Stall on memory reclaim and on block I/O is invisible to
`top` and absorbed indistinguishably into load average, but appears directly in
`/proc/pressure/memory` and `/proc/pressure/io`. For capacity planning, a `full`
figure trending upward across weeks names both the exhausted resource and the
magnitude of the loss as a percentage of time.

**Try next:** run `stress-ng --vm 4 --vm-bytes 90% --timeout 30s` in one
terminal and `watch -n1 cat /proc/pressure/memory` in another, and observe
`full` rise as reclaim thrashes; then place the stressor in a dedicated cgroup
and confirm the pressure appears in that cgroup's `memory.pressure` while a
sibling cgroup stays at zero.

## Pitfalls

- **Alerting on `avg10` produces flapping alerts.** The 10-second window is
  dominated by short reclaim bursts and brief I/O queues that resolve without
  intervention; `avg60` or `avg300` is the stable input for a paging threshold.
- **Differencing the `avgN` fields yields meaningless quantities.** They are
  decaying averages, not counters. Only `total`, in microseconds, may be
  subtracted between two samples to obtain stall time over an interval.
- **Expecting a `full` line for whole-system CPU pressure misreads the file.**
  A state in which every task is stalled on CPU while the CPU is available is
  not reachable, so CPU pressure must be alerted on via `some`.
- **Reading a cgroup's pressure as evidence of that cgroup's misbehaviour is
  unsound.** The file records stall suffered by the cgroup's tasks, which may be
  imposed entirely by a competing consumer of the same device or memory.
- **A trigger disappears silently when its file descriptor is closed.** A
  supervisor that reopens the pressure file on each iteration, or that lets the
  descriptor go out of scope, registers a trigger that never fires again and
  reports no error.
- **A trigger threshold set close to the OOM condition removes the advantage.**
  The mechanism is useful only when the stall budget is crossed while reclaim is
  still succeeding; a budget chosen so high that it coincides with allocation
  failure leaves no interval in which userspace can act first.
