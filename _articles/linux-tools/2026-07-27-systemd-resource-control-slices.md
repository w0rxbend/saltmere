---
title: "Let systemd hold the leash: cgroups v2 with slices, scopes, and set-property"
date: 2026-07-27
track: linux-tools
summary: "Poking /sys/fs/cgroup by hand teaches you the kernel knobs, but in production systemd owns the hierarchy. Here's how to cap CPU, memory, I/O, and tasks through slices, scopes, and drop-ins — and watch it work."
reading_time: 6
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

In an earlier post I built a memory jail by hand — `mkdir` under `/sys/fs/cgroup`, echo a number into `memory.max`, watch the OOM killer fire. That's the right way to learn the kernel primitives. But you almost never touch cgroupfs directly on a real box, because something else already owns it: **systemd is the cgroup manager**. Write to `memory.max` yourself and systemd may clobber it on the next reload. So the production skill isn't `echo`-into-sysfs; it's telling systemd what you want and letting it drive the hierarchy.

## The three unit types that carry limits

systemd organizes every process into a tree of cgroups made of three unit kinds:

- **Slices** (`*.slice`) are the branches — organizational containers that hold other units and let you apply limits to a whole group. Boot gives you `system.slice` (daemons), `user.slice` (login sessions), and `machine.slice` (VMs/containers).
- **Services** (`*.service`) are daemons systemd starts for you; each gets its own leaf cgroup.
- **Scopes** (`*.scope`) are transient groups wrapped around processes systemd *didn't* fork itself — a login session, or a one-off command you sandbox.

Because cgroups v2 forbids "internal processes," only leaves (services and scopes) hold tasks; slices are pure structure. Every resource directive below is documented in `systemd.resource-control(5)` and works identically whether you set it on a slice, service, or scope.

## The directives that matter

| Directive | Effect |
|---|---|
| `CPUWeight=` | Relative share (1–10000, default 100) split among siblings in a slice |
| `CPUQuota=` | Absolute cap; `20%` = one-fifth of a core, `200%` = two full cores |
| `MemoryHigh=` | Soft throttle: reclaim hard, stall the process, but don't kill |
| `MemoryMax=` | Hard wall: cross it and the OOM killer fires inside the unit |
| `IOWeight=` | Relative block-I/O bandwidth share (1–10000, default 100) |
| `TasksMax=` | Cap on number of tasks (PIDs); a number, a `%`, or `infinity` |

Two distinctions are worth getting exactly right.

**`CPUWeight=` vs `CPUQuota=`.** Weight is *proportional and only bites under contention* — two units at weight 100 and 200 split a saturated CPU 1:2, but if the machine is idle either can use everything. Quota is an *absolute ceiling that always applies*: `CPUQuota=20%` never lets the unit exceed 20% of a single CPU's time even on an empty box. Use weight to prioritize; use quota to cap.

**`MemoryHigh=` vs `MemoryMax=`.** `MemoryHigh=` is the main throttle — above it the kernel reclaims aggressively and slows the process to hold the line, but tolerates brief overshoot. `MemoryMax=` is the last line of defense; usage that can't be contained under it triggers an in-cgroup OOM kill. The documented pattern is to set `MemoryHigh=` as your working limit and `MemoryMax=` a bit higher as a hard backstop, so you get back-pressure before a cliff.

## Sandbox a one-off command

`systemd-run` wraps an arbitrary command in a transient scope with limits attached — no unit file, gone when it exits:

```bash
systemd-run --scope -p CPUQuota=20% -p MemoryMax=256M -p MemoryHigh=200M \
  -p TasksMax=64 stress-ng --vm 2 --vm-bytes 512M --timeout 60s
```

`--scope` runs it in your terminal (foreground); drop it and you get a transient `.service` running in the background instead. This is the safe way to run a risky import, a runaway build, or a load test without threatening the rest of the machine.

## Constrain a real service

For a managed service, don't hand-edit the vendor's unit file. Set properties at runtime — systemd writes a persistent drop-in for you under `/etc/systemd/system.control/`:

```bash
sudo systemctl set-property nginx.service CPUQuota=150% MemoryMax=1G IOWeight=200
```

That survives reboots and applies live. For settings you want in version control, write your own drop-in instead:

```ini
# /etc/systemd/system/nginx.service.d/10-limits.conf
[Service]
CPUWeight=200
MemoryHigh=800M
MemoryMax=1G
TasksMax=512
```

Then `sudo systemctl daemon-reload && sudo systemctl restart nginx`. Drop-ins layer over the base unit in lexicographic order, so `10-limits.conf` beats defaults without you forking the original. You can also cap an entire slice — put `MemoryMax=4G` on a custom `my-apps.slice` and every service assigned `Slice=my-apps.slice` shares that ceiling.

## Watch it actually work

Two tools read the live hierarchy. `systemd-cgls` prints the tree:

```bash
systemd-cgls
# └─system.slice
#   ├─nginx.service …
#   └─run-r9f3.scope   ← your systemd-run sandbox
```

`systemd-cgtop` is `top` for cgroups — sort by CPU, memory, or I/O and watch your caps hold:

```bash
systemd-cgtop -m --order=memory
```

To confirm a specific limit landed, ask systemd what it thinks the value is rather than reading sysfs:

```bash
systemctl show nginx.service -p CPUQuotaPerSecUSec -p MemoryMax -p MemoryCurrent
```

Every one of these directives ultimately writes the same `cpu.max`, `memory.max`, `memory.high`, `io.weight`, and `pids.max` files you'd poke by hand — you've just handed the pen to the process that's supposed to be holding it.

**Try next:** launch two `systemd-run --scope` sandboxes in one slice with `CPUWeight=100` and `CPUWeight=300`, pin both with `stress-ng --cpu 0`, and watch `systemd-cgtop` split a saturated CPU 1:3 — then add `CPUQuota=25%` to one and see the ceiling override the share.
