---
title: "PSI: the kernel telling you how much time you lost to contention"
date: 2026-07-30
track: linux-tools
summary: "Load average lies — it can't tell 'busy' from 'stalled waiting for memory or disk.' Pressure Stall Information gives you the real number: what fraction of wall-clock time tasks spent blocked on CPU, memory, or I/O. Here's how to read /proc/pressure, scope it per-cgroup, and set a poll trigger that fires before OOM does."
reading_time: 5
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

Load average is the metric everyone reaches for and the one that answers the wrong question. A load of 8 on an 8-core box could mean "perfectly utilized" or "utterly wedged waiting for disk" — the number can't distinguish work from waiting. Pressure Stall Information (PSI), merged in Linux 4.20 and now the backbone of Facebook/Meta's `oomd`, measures the thing you actually care about: **how much wall-clock time did tasks lose because a resource wasn't available.**

## Reading the three files

PSI exposes CPU, memory, and I/O under `/proc/pressure/`:

```
$ cat /proc/pressure/memory
some avg10=0.00 avg60=0.12 avg300=0.05 total=8419283
full avg10=0.00 avg60=0.08 avg300=0.03 total=4210394
```

Two lines, and the distinction between them is the whole insight:

- **`some`** — the fraction of time *at least one* task was stalled waiting for the resource. This is your *latency* signal: work is being delayed.
- **`full`** — the fraction of time *every* runnable task was stalled simultaneously, so *nothing* useful happened. This is your *lost throughput* signal — the machine (or cgroup) was effectively frozen on that resource.

`avg10/60/300` are percentages over the last 10, 60, and 300 seconds; `total` is cumulative microseconds of stall. (CPU has no `full` line — if the CPU is the contended resource, by definition *something* is running on it.)

The interpretation is direct and quantitative in a way load average never is: `memory full avg60=20.00` means that over the last minute, for 20% of the time, your whole workload was stalled reclaiming memory instead of doing work. That's not "memory looks a bit high" — that's "you lost 12 seconds of the last 60 to memory pressure."

## Per-cgroup pressure is where it earns its keep

The global files are useful, but the real power is that PSI is accounted **per cgroup v2**. Every cgroup has its own `cpu.pressure`, `memory.pressure`, and `io.pressure`:

```
$ cat /sys/fs/cgroup/system.slice/my-app.service/memory.pressure
some avg10=4.21 avg60=2.90 avg300=1.10 total=99182734
full avg10=1.80 avg60=1.02 avg300=0.44 total=41028734
```

Now you can attribute pressure to a *specific service*. This is exactly how Kubernetes surfaces per-pod and per-node PSI, and how you answer "is *this* container the one thrashing?" without guessing from aggregate host metrics. A noisy neighbor that stalls its own cgroup shows up here even when the host's global numbers look calm.

## The killer feature: poll before it's too late

Polling `cat` in a loop is fine for eyeballing, but PSI's real trick is that you can register a **trigger** and have the kernel wake you the instant pressure crosses a threshold. You write a threshold to the pressure file and `poll()` on the fd:

```c
// Wake me if 'some' memory stall exceeds 150ms within any 1s window.
int fd = open("/proc/pressure/memory", O_RDWR | O_NONBLOCK);
const char *trig = "some 150000 1000000";   // stall_us window_us
write(fd, trig, strlen(trig));

struct pollfd pfd = { .fd = fd, .events = POLLPRI };
while (poll(&pfd, 1, -1) > 0) {
    // Fired: memory pressure just crossed the line.
    // Shed load, drop caches, or kill the worst offender — BEFORE the OOM killer does.
    handle_memory_pressure();
}
```

That "before the OOM killer does" is the point. The kernel OOM killer is a blunt, last-resort instrument that fires when memory is *already* exhausted, often killing the wrong process. A PSI trigger lets *userspace* react to the *approach* of exhaustion — Meta's `oomd` and systemd-oomd both work exactly this way, watching PSI and taking graceful action (killing a chosen cgroup, shedding load) seconds before the kernel would panic-kill something. You get to choose the victim, on your terms, with time to spare.

## Where to reach for it

Any time you're diagnosing "the box feels slow but CPU looks fine," check `/proc/pressure/io` and `/proc/pressure/memory` first — stall on those resources is invisible to `top` and load average but obvious in PSI. For capacity planning, `full` pressure trending up over weeks is your early warning that a tier is running out of headroom on a specific resource, told to you as a percentage of lost time rather than a proxy you have to interpret.

**Try next:** Run `stress-ng --vm 4 --vm-bytes 90% --timeout 30s` in one terminal and `watch -n1 cat /proc/pressure/memory` in another; watch `full` climb as reclaim thrashes — then move the stressor into its own cgroup and confirm the pressure shows up in *that* cgroup's `memory.pressure` while a sibling cgroup stays clean. That per-cgroup attribution is the thing load average can never give you.
