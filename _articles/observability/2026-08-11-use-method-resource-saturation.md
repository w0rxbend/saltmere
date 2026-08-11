---
title: "The USE method: find the bottleneck before you find the graph"
date: 2026-08-11
track: observability
summary: "Brendan Gregg's USE method is a checklist: for every resource, measure Utilization, Saturation, and Errors. Saturation is the one most dashboards miss — a CPU can read 100% busy and still be fine, but a growing run queue means work is piling up. Here's the full checklist on Linux with real tools, the PromQL to alert on saturation, and where USE ends and RED begins."
reading_time: 6
tags: [use-method, saturation, prometheus, node-exporter, psi, performance]
sources:
  - title: "The USE Method — Brendan Gregg"
    url: "https://www.brendangregg.com/usemethod.html"
  - title: "USE Method: Linux Performance Checklist — Brendan Gregg"
    url: "https://www.brendangregg.com/USEmethod/use-linux.html"
  - title: "The RED Method: how to instrument your services — Grafana"
    url: "https://grafana.com/blog/2018/08/02/the-red-method-how-to-instrument-your-services/"
  - title: "PSI - Pressure Stall Information — The Linux Kernel documentation"
    url: "https://docs.kernel.org/accounting/psi.html"
  - title: "Monitoring Linux host metrics with the Node Exporter — Prometheus"
    url: "https://prometheus.io/docs/guides/node-exporter/"
---

Most performance investigations start from the wrong end: someone opens a dashboard, scrolls until a line looks angry, and works backward. The USE method inverts that. Instead of starting from graphs you already have, you start from a list of every resource in the system and interrogate each one the same way. Brendan Gregg's formulation is deliberately mechanical: for every resource, check its **U**tilization, **S**aturation, and **E**rrors. It is a checklist you can run in a few minutes that reliably surfaces the actual bottleneck rather than the loudest symptom.

## The three questions

A *resource* here is a physical component with finite capacity: CPUs, memory capacity, network interfaces, storage devices, and the interconnects between them. For each one you ask:

- **Utilization** — "the average time that the resource was busy servicing work." Usually a percentage: CPU busy 70%, disk busy 40%, a NIC at 3 Gbit of a 10 Gbit link.
- **Saturation** — "the degree to which the resource has extra work which it can't service, often queued." This is queue depth: the run queue, the disk I/O queue, memory reclaim pressure. Gregg's rule is blunt — any non-zero saturation can be a problem.
- **Errors** — "the count of error events." NIC drops, disk media errors, failed allocations. Errors get their own line because they degrade performance quietly, especially when the failure mode is recoverable and retried.

## Why saturation is the one you're missing

Utilization is seductive because it is easy to collect and easy to plot, but it lies by omission. A CPU averaged at 80% over a minute looks like it has headroom, yet if that minute contained repeated 100% spikes, requests were queuing the whole time — Gregg tells exactly this story of customers hitting CPU saturation while the monitoring showed a comfortable 80%. Utilization is a bounded number: it maxes out at 100% and then tells you nothing more. Saturation has no ceiling. A run queue of 4 and a run queue of 40 both report "100% utilized," but only saturation distinguishes *busy* from *drowning*.

This is exactly the gap [Pressure Stall Information](/articles/linux-tools/2026-07-30-psi-pressure-stall-information) was built to close. PSI measures the fraction of wall-clock time tasks spent stalled waiting for a resource, which is a direct, modern saturation signal — far better than inferring it from load average.

## The checklist on Linux

Here is the walk-through for the four resources you will actually chase. Every cell is a real tool.

| Resource | Utilization | Saturation | Errors |
|---|---|---|---|
| CPU | `mpstat -P ALL 1`, `top` | `vmstat 1` **r** column; `/proc/pressure/cpu` | rare (rely on `mcelog`) |
| Memory | `free -m`, `sar -r` | `vmstat` **si**/**so**; `/proc/pressure/memory`; OOM kills | `dmesg` (ECC, killed) |
| Disk | `iostat -xz 1` **%util** | `iostat` **aqu-sz** > 1, high **await** | `smartctl`, `/sys/.../ioerr_cnt` |
| Network | `sar -n DEV 1` | `netstat -s` retransmits; NIC overruns | `ip -s link` errors/dropped |

Run it top to bottom. For **CPU**, `mpstat` gives you per-core utilization; then `vmstat 1` and read the `r` column — that is the number of threads runnable but waiting for a core. If `r` exceeds your core count, you are CPU-saturated regardless of what utilization says. Confirm with the PSI file:

```bash
$ cat /proc/pressure/cpu
some avg10=17.24 avg60=9.81 avg300=4.02 total=843211904
```

`some avg10=17.24` means that over the last 10 seconds, tasks were stalled waiting for CPU 17% of the time. Non-zero and climbing is your saturation alarm.

For **memory**, `free -m` is utilization; the saturation signal is *reclaim*, not usage. Watch `si`/`so` in `vmstat` for swap-in/swap-out activity, check `/proc/pressure/memory`, and grep `dmesg` for the OOM killer. A box can sit at 95% memory utilization forever and be perfectly healthy; the moment it starts swapping, saturation has begun. **Disk** and **network** follow the same shape: `%util` and throughput for utilization, queue length and retransmits for saturation, media and drop counters for errors.

## The same checklist in Prometheus

`node_exporter` exposes everything above, so the checklist becomes alertable. CPU utilization is the idle complement:

```promql
# CPU utilization %, per instance
100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)

# CPU saturation: fraction of time stalled waiting for CPU (PSI)
rate(node_pressure_cpu_waiting_seconds_total[5m])

# Memory saturation: time stalled on memory reclaim (PSI)
rate(node_pressure_memory_stalled_seconds_total[5m])
```

Alert on the utilization query and you page for busy machines. Alert on the two saturation queries — a PSI rate creeping toward 1.0 means a full second of every second is lost to waiting — and you page for machines that are actually failing to keep up. That distinction is the whole point of separating the two lines.

## USE for machines, RED for services

USE is resource-oriented, which is precisely why it does not fit services. Tom Wilkie built the [RED method](/articles/observability/2026-07-30-spanmetrics-connector-red-metrics) — **R**ate, **E**rrors, **D**uration — as the request-oriented complement, and put the split plainly: "The RED Method is about caring about your users and how happy they are, and the USE Method is about caring about your machines and how happy they are." A service has no "utilization"; it has a request rate and a latency distribution. So use RED at the top of the stack to prove users are hurting, then drop to USE on the underlying hosts to find which resource is the cause. Latency spiking (RED Duration) plus a growing run queue (USE Saturation) on the same node is a complete story, told from both ends.

**Try next:** open `/proc/pressure/cpu`, `/proc/pressure/memory`, and `/proc/pressure/io` on a loaded host, then compare the `avg10` numbers against what your utilization dashboard claims — the gap between them is the saturation your graphs have been hiding.
