---
title: "The USE method: locating the bottleneck before opening a dashboard"
date: 2026-08-11
track: observability
summary: "Brendan Gregg's USE method is a checklist: for every resource, measure Utilization, Saturation, and Errors. Saturation is the term most dashboards omit — a CPU can read 100% busy and still be healthy, but a growing run queue means work is accumulating. This article gives the checklist on Linux with real tools, the PromQL for alerting on saturation, and the boundary where USE ends and RED begins."
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

**Gist.** Performance investigations that begin from an existing dashboard find the loudest symptom rather than the constraining resource, because the dashboard shows whatever someone once chose to plot. The USE method inverts the search order: enumerate every resource in the system first, then interrogate each one for **U**tilization, **S**aturation, and **E**rrors, in that fixed order. The cost is that the enumeration must be built and maintained by hand — the method supplies no resource inventory, and a resource absent from the list is a bottleneck the checklist cannot find.

## The three terms

A *resource* in Gregg's formulation is a physical component with finite capacity: central processing units (CPUs), memory capacity, network interfaces, storage devices, and the interconnects between them. Each is interrogated with the same three questions.

- **Utilization** — "the average time that the resource was busy servicing work." Normally a percentage: CPU busy 70%, disk busy 40%, a network interface card (NIC) carrying 3 Gbit of a 10 Gbit link.
- **Saturation** — "the degree to which the resource has extra work which it can't service, often queued." This is queue depth: the run queue, the disk input/output (I/O) queue, memory reclaim pressure. **Gregg states that any level of saturation can be a problem, non-zero being the threshold of interest.**
- **Errors** — "the count of error events." NIC drops, disk media errors, failed allocations. Errors occupy a separate line because a recoverable-and-retried failure degrades throughput without changing either of the other two numbers in an obvious way.

## Why the saturation term is not redundant with utilization

Utilization is collected and plotted easily, which is why it dominates dashboards, and it misleads by omission in two distinct ways.

The first is **averaging over the observation window**. A CPU reported at 80% across one minute appears to have headroom, yet the same average is produced by a minute containing repeated intervals at 100% — during which work was queuing. Gregg notes this caveat directly: a utilization figure is an average over an interval, and short bursts of full utilization disappear into it.

The second is **the ceiling**. Utilization is bounded: it reaches 100% and then carries no further information. Saturation is unbounded. A run queue of 4 and a run queue of 40 both report "100% utilized"; only the saturation term separates *busy* from *unable to keep up*. This is the property that makes saturation the leading indicator — utilization saturates as a signal before the resource does.

[Pressure Stall Information](/articles/linux-tools/2026-07-30-psi-pressure-stall-information) closes this gap directly. PSI measures **the fraction of wall-clock time tasks spent stalled waiting for a resource**, which is a saturation measurement rather than an inference drawn from load average.

## The checklist on Linux

Four resources cover the common cases. Every cell below names a real tool.

| Resource | Utilization | Saturation | Errors |
|---|---|---|---|
| CPU | `mpstat -P ALL 1`, `top` | `vmstat 1` **r** column; `/proc/pressure/cpu` | rare (rely on `mcelog`) |
| Memory | `free -m`, `sar -r` | `vmstat` **si**/**so**; `/proc/pressure/memory`; OOM kills | `dmesg` (ECC, killed) |
| Disk | `iostat -xz 1` **%util** | `iostat` **aqu-sz**, **await** | `smartctl`, `/sys/.../ioerr_cnt` |
| Network | `sar -n DEV 1` | `netstat -s` retransmits; NIC overruns | `ip -s link` errors/dropped |

The table is walked top to bottom rather than sampled. For **CPU**, `mpstat` reports per-core utilization; `vmstat 1` then supplies the `r` column, which counts **threads that are runnable but waiting for a core**. When `r` exceeds the core count, the CPU is saturated irrespective of the utilization figure. PSI confirms it:

```bash
$ cat /proc/pressure/cpu
some avg10=17.24 avg60=9.81 avg300=4.02 total=843211904
```

`some avg10=17.24` states that over the preceding 10 seconds, tasks were stalled waiting for CPU 17% of the time. A non-zero and climbing value is the saturation alarm.

For **memory**, `free -m` supplies utilization, but **the saturation signal is reclaim activity, not occupancy**. The relevant observations are `si`/`so` in `vmstat` for swap-in and swap-out traffic, `/proc/pressure/memory`, and out-of-memory (OOM) killer records in `dmesg`. A host can sit at 95% memory utilization indefinitely in good health; the onset of swapping is the onset of saturation. **Disk** and **network** follow the identical shape: `%util` and throughput for utilization, queue length and retransmission counts for saturation, media and drop counters for errors.

## The same checklist expressed in Prometheus

`node_exporter` exposes each of the quantities above, so the checklist becomes alertable. CPU utilization is derived as the complement of idle time:

```promql
# CPU utilization %, per instance
100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)

# CPU saturation: fraction of time stalled waiting for CPU (PSI)
rate(node_pressure_cpu_waiting_seconds_total[5m])

# Memory saturation: time stalled on memory reclaim (PSI)
rate(node_pressure_memory_stalled_seconds_total[5m])
```

The three queries carry different operational meanings and warrant different alert policies. An alert on the first pages for busy machines, a condition that may be the intended steady state of the fleet. Alerts on the latter two page for machines failing to keep up: **a PSI rate approaching 1.0 means a full second of stall accumulates per second of wall-clock time.** Keeping the utilization query separate from the saturation queries is what makes that distinction expressible in an alert rule.

Because the PSI series are counters of stalled seconds, `rate()` over them yields a dimensionless fraction between 0 and 1, and the window (`[5m]` above) sets the averaging interval. The averaging problem described earlier reappears here: a five-minute rate smooths a burst the same way a one-minute utilization average does. The kernel's own `avg10` field is the shortest published window and is the one to consult when the concern is bursts rather than sustained pressure.

## USE for machines, RED for services

USE is resource-oriented, which is the reason it does not transfer to services. Tom Wilkie's [RED method](/articles/observability/2026-07-30-spanmetrics-connector-red-metrics) — **R**ate, **E**rrors, **D**uration — is the request-oriented complement. The Grafana write-up frames RED as the request-side counterpart to USE: RED describes a service from the perspective of the requests flowing through it, USE describes the resources underneath it.

The asymmetry is structural. A service has no utilization term, because a service has no fixed capacity to be busy against; it has a request rate and a latency distribution. The two methods therefore compose in one direction: RED at the top of the stack establishes that users are affected, and USE on the underlying hosts identifies which resource is responsible. Rising latency (RED Duration) alongside a growing run queue (USE Saturation) on the same node is a single account of one incident, evidenced from both ends.

## Pitfalls

- **Alerting only on utilization.** A fleet held at 80% CPU by design pages constantly while a machine at 100% with a run queue of 40 pages identically to one at 100% with a run queue of 2 — the alert cannot distinguish healthy from failing.
- **Reading memory saturation off `free -m`.** High memory occupancy is the expected state on a host with an active page cache; the saturation evidence is swap traffic in `vmstat` and `/proc/pressure/memory`, and absent those the utilization figure carries no alarm.
- **Long rate windows hiding bursts.** `rate(...[5m])` on a PSI counter reproduces the averaging defect that motivated the saturation term in the first place; sub-window spikes are flattened into a benign mean.
- **Treating errors as covered by the other two terms.** A retried NIC drop or a recoverable media error consumes capacity without raising utilization in a legible way, which is why USE counts errors separately.
- **An incomplete resource inventory.** The method finds bottlenecks only among the resources enumerated; a component absent from the list — a saturated interconnect, an exhausted file-descriptor limit — is invisible to a checklist run that never mentions it.
- **Applying USE to a service.** A request handler has no utilization term to fill in, and forcing one produces a number that does not correspond to any finite capacity; RED covers that layer.
