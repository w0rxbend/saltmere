---
title: "systemd as cgroup manager: slices, scopes, and resource-control properties"
date: 2026-07-27
summary: "On a running Linux system systemd owns the control-group hierarchy. This article covers capping CPU, memory, block I/O, and task count through slices, scopes, drop-ins, and set-property, and verifying the result."
track: linux-tools
reading_time: 7
tags: [systemd, cgroups-v2, resource-control, linux, sandboxing]
sources:
  - title: "systemd.resource-control(5) — Linux manual page"
    url: "https://man7.org/linux/man-pages/man5/systemd.resource-control.5.html"
  - title: "systemd-run(1) — Linux manual page"
    url: "https://man7.org/linux/man-pages/man1/systemd-run.1.html"
  - title: "Oracle Linux 9: Using systemd to Manage cgroups v2"
    url: "https://docs.oracle.com/en/operating-systems/oracle-linux/9/systemd/SystemdMngCgroupsV2.html"
  - title: "Red Hat: Managing cgroups with systemd (cgroups part four)"
    url: "https://www.redhat.com/en/blog/cgroups-part-four"
---

**Gist.** Control groups version 2 (cgroups v2) expose their limits as files under `/sys/fs/cgroup`, but on a system running systemd those files have a single writer: **systemd is the cgroup manager**, and it reasserts the values it believes in whenever a unit is started or its configuration reloaded. Limits therefore have to be expressed as unit properties — on a slice, a service, or a transient scope — rather than written into cgroupfs by hand. The cost is indirection: the effective value must be read back from systemd's own view, because the number a manual `echo` placed in `memory.max` can be overwritten without notice.

## The three unit types that carry limits

systemd arranges every process into a tree of control groups built from three kinds of unit.

- **Slices** (`*.slice`) are the interior nodes: organizational containers that hold other units and carry limits applying to the whole subtree. systemd defines `system.slice` for system services, `user.slice` for user sessions, and `machine.slice` for virtual machines and containers, all below the root slice `-.slice`.
- **Services** (`*.service`) are units systemd starts itself; each occupies its own leaf control group.
- **Scopes** (`*.scope`) are transient groups wrapped around processes systemd did **not** fork — a login session, or a single command placed under limits after the fact.

cgroups v2 enforces the **no-internal-processes rule**: below the root, a control group may hold tasks or hold child groups with controllers enabled, not both. Slices are consequently pure structure and never contain processes directly; only services and scopes are leaves. Every directive below is documented in `systemd.resource-control(5)` and is accepted identically on a slice, a service, or a scope.

## The directives

| Directive | Effect |
|---|---|
| `CPUWeight=` | Relative share (1–10000, default 100) split among siblings within a slice |
| `CPUQuota=` | Absolute cap; `20%` is one fifth of one CPU, `200%` is two full CPUs |
| `MemoryHigh=` | Throttle: reclaim is intensified and the process is stalled, but not killed |
| `MemoryMax=` | Hard limit; usage that cannot be reclaimed below it triggers an in-cgroup OOM kill |
| `IOWeight=` | Relative block-I/O bandwidth share (1–10000, default 100) |
| `TasksMax=` | Cap on task (PID) count: an absolute number, a percentage, or `infinity` |

Two distinctions decide whether a configuration behaves as intended.

**`CPUWeight=` against `CPUQuota=`.** Weight is proportional and **binds only under contention**: two sibling units at weight 100 and 200 divide a saturated CPU in the ratio 1:2, but on an otherwise idle machine either unit may consume everything. Quota is an absolute ceiling that **applies unconditionally**; `CPUQuota=20%` prevents the unit from exceeding one fifth of a single CPU's time even when nothing else is runnable. Weight expresses priority; quota expresses a bound.

**`MemoryHigh=` against `MemoryMax=`.** Above `MemoryHigh=` the kernel reclaims aggressively and throttles the allocating process, which holds usage near the line while tolerating brief overshoot — the process continues to run. `MemoryMax=` admits no overshoot: when reclaim cannot bring usage back under the limit, the OOM killer fires **inside the unit**, so the casualty is a process of that unit rather than an unrelated process elsewhere on the machine. `systemd.resource-control(5)` recommends `MemoryHigh=` as the main mechanism of memory control and `MemoryMax=` as a last line of defence, an ordering that produces back-pressure before termination becomes the only remaining mechanism.

## Constraining a single command

`systemd-run` wraps an arbitrary command in a transient unit with properties attached, requiring no unit file and leaving nothing behind once the command exits:

```bash
systemd-run --scope -p CPUQuota=20% -p MemoryMax=256M -p MemoryHigh=200M \
  -p TasksMax=64 stress-ng --vm 2 --vm-bytes 512M --timeout 60s
```

`--scope` runs the command in the invoking terminal in the foreground. Without it, `systemd-run` creates a transient `.service` instead, forked by systemd and running in the background. The distinction matters for signal delivery and for where standard output lands, not for the limits, which apply the same way in both cases.

## Constraining a managed service

Editing a vendor-supplied unit file in `/usr/lib/systemd/system` is not required and is overwritten by package upgrades. Properties can instead be set at runtime, in which case systemd writes a persistent drop-in under `/etc/systemd/system.control/`:

```bash
sudo systemctl set-property nginx.service CPUQuota=150% MemoryMax=1G IOWeight=200
```

The change applies to the running unit and persists across reboot. For configuration that belongs in version control, an explicit drop-in is the alternative:

```ini
# /etc/systemd/system/nginx.service.d/10-limits.conf
[Service]
CPUWeight=200
MemoryHigh=800M
MemoryMax=1G
TasksMax=512
```

followed by `sudo systemctl daemon-reload && sudo systemctl restart nginx`. **Drop-ins layer over the base unit in lexicographic filename order**, so `10-limits.conf` overrides the shipped defaults without the original file being forked or modified.

Limits also compose downward. `MemoryMax=4G` on a custom `my-apps.slice` bounds the aggregate usage of every unit declaring `Slice=my-apps.slice`; each member may additionally carry its own, tighter limit. The effective constraint at any point is the tightest limit along the path from the root to the leaf — a service permitted 3 GiB inside a 4 GiB slice is still confined to whatever the slice has left after its siblings.

## Verification

Two tools read the live hierarchy. `systemd-cgls` prints the tree:

```bash
systemd-cgls
# └─system.slice
#   ├─nginx.service …
#   └─run-r9f3.scope   ← the transient systemd-run unit
```

`systemd-cgtop` is the control-group analogue of `top` and sorts by CPU, memory, or I/O:

```bash
systemd-cgtop --order=memory
```

Confirming that a particular limit took effect is done by querying systemd's own view of the unit rather than by reading cgroupfs, since systemd's view is what will be reapplied on the next reload:

```bash
systemctl show nginx.service -p CPUQuotaPerSecUSec -p MemoryMax -p MemoryCurrent
```

Each directive ultimately writes the same `cpu.max`, `memory.max`, `memory.high`, `io.weight` and `pids.max` files that a manual configuration would target. The difference is ownership of the write.

An instructive experiment: start two `systemd-run --scope` units in one slice with `CPUWeight=100` and `CPUWeight=300`, saturate both with `stress-ng --cpu 0`, and observe `systemd-cgtop` divide the saturated CPU time in the ratio 1:3. Adding `CPUQuota=25%` to the heavier unit then demonstrates the ceiling taking precedence over the proportional share.

## Pitfalls

- **Writing to `/sys/fs/cgroup` directly on a systemd host.** The value survives until the unit is restarted or the manager configuration is reloaded, at which point systemd reasserts the property it holds and the manual value disappears — a limit that appears to work in testing and vanishes at the next `daemon-reload`.
- **Assuming `CPUWeight=` caps anything.** A unit at weight 1 alone on an idle machine consumes every available CPU; weight redistributes only when siblings compete. A workload that must never exceed a fraction of a CPU requires `CPUQuota=`.
- **Setting `MemoryMax=` without `MemoryHigh=`.** The unit runs at full speed up to the limit and is then OOM-killed with no throttling phase in between, so the first observable symptom is a dead process rather than degraded throughput.
- **Attempting to place a process directly in a slice.** cgroups v2 forbids a non-root group from holding tasks while distributing controllers to child groups, so processes belong to services and scopes only; a slice is structure.
- **Editing the packaged unit file instead of adding a drop-in.** A package upgrade replaces the file and silently removes the limits.
- **Reading `MemoryCurrent` and concluding the limit is inactive.** `MemoryCurrent` reports present usage, not the configured bound; `MemoryMax` and `CPUQuotaPerSecUSec` are the properties that report the configured values.
- **Expecting `set-property` changes to appear in the unit's own drop-in directory.** They are written under `/etc/systemd/system.control/`, which is separate from `/etc/systemd/system/<unit>.d/`, so a configuration audit that inspects only the latter will miss them.
