---
title: "Tetragon: eBPF Runtime Security That Filters and Kills In-Kernel"
date: 2026-07-31
track: linux-tools
summary: "Tetragon hooks the kernel with eBPF to observe process lineage, file access, and syscalls — and unlike alert-only tools, it can filter and enforce (SIGKILL, override) entirely in-kernel, with no userspace round-trip. Here's the architecture, a standalone Docker run, and a TracingPolicy that kills a process reading /etc/shadow."
reading_time: 6
tags: [ebpf, security, tetragon, cilium, observability, linux]
sources:
  - title: "Tetragon — Policy Enforcement (docs)"
    url: "https://tetragon.io/docs/getting-started/enforcement/"
  - title: "Tetragon — Tracing Policy concept"
    url: "https://tetragon.io/docs/concepts/tracing-policy/"
  - title: "cilium/tetragon — GitHub releases"
    url: "https://github.com/cilium/tetragon/releases"
  - title: "file_monitoring_enforce.yaml — official enforce policy example"
    url: "https://github.com/cilium/tetragon/blob/main/examples/quickstart/file_monitoring_enforce.yaml"
  - title: "CNCF — Cilium Graduation announcement (Oct 11, 2023)"
    url: "https://www.cncf.io/announcements/2023/10/11/cloud-native-computing-foundation-announces-cilium-graduation/"
---

Most runtime security tools watch the kernel with eBPF, ship the raw events up to a userspace engine, evaluate rules there, and — when something looks bad — fire an alert. **Tetragon** moves both the filtering *and* the response into the kernel. It decides whether an event matters before it ever crosses into userspace, and it can act on a match synchronously: kill the process, override a syscall's return value, send a signal. That single architectural choice is what separates "I got paged about the breach" from "the read never completed."

Tetragon comes from Isovalent (the Cilium/eBPF team, now part of Cisco) and rides under Cilium, a **CNCF Graduated** project since October 2023. It hit **1.0 in November 2023** and the line is actively developed — the 1.7 release landed in 2026, adding things like CEL expressions and environment-variable retrieval in BPF.

## How it's wired

eBPF programs attach to **kprobes** (arbitrary kernel functions), **syscalls**, tracepoints, uprobes, and **LSM hooks**. When one fires, the event flows kernel-to-userspace over a ring buffer to the Tetragon agent, which enriches it with full process context: the executable, arguments, and crucially the **entire ancestry** — this process, its parent, the container and pod it belongs to (resolved via cgroups). You don't just see "something opened a file," you see the whole exec chain that led there.

The performance trick is that **filtering happens inside eBPF**. A policy that only cares about writes to `/etc`, or execs of a specific binary, encodes those predicates into the BPF program, so non-matching events are dropped in-kernel and never pay the cost of a userspace crossing. Enforcement runs there too: a matched selector can carry `Sigkill` to terminate the offending process, or `Override` to force a syscall to return an error, all without a round-trip.

That's the concrete difference from **Falco**, the other big eBPF security tool. Falco captures syscalls with eBPF but evaluates its rules in *userspace* and, by design, only *detects* — it emits an alert and leaves blocking to external tooling. Falco has the larger community rule ecosystem; Tetragon's edge is in-kernel filtering (lower overhead), in-kernel enforcement (real prevention), and richer process lineage. Different tools, and plenty of shops run both.

## Running it standalone

Tetragon needs no Kubernetes. As a plain container it wants host PID and cgroup namespaces and the kernel BTF for CO-RE:

```bash
docker run --name tetragon --rm -d \
    --pid=host --cgroupns=host --privileged \
    -v /sys/kernel/btf/vmlinux:/var/lib/tetragon/btf \
    quay.io/cilium/tetragon:v1.7.0
```

Then watch the live event stream, process lineage and all:

```bash
docker exec -ti tetragon tetra getevents -o compact
```

Out of the box that's exec/exit visibility across the host. The interesting part is policy.

## A policy that filters — and kills — in-kernel

A **TracingPolicy** describes what to hook, what to match, and what to do. This one hooks the kernel's `security_file_permission` LSM function, filters in-kernel to a few sensitive paths plus read access, and terminates any process that tries:

```yaml
apiVersion: cilium.io/v1alpha1
kind: TracingPolicyNamespaced
metadata:
  name: "file-monitoring-filtered"
spec:
  kprobes:
  - call: "security_file_permission"
    syscall: false
    return: true
    args:
    - index: 0
      type: "file"
    - index: 1
      type: "int"
    returnArg:
      index: 0
      type: "int"
    selectors:
    - matchArgs:
      - index: 0
        operator: "Prefix"
        values:
        - "/etc/shadow"
        - "/root/.ssh"
        - "/etc/sudoers"
      - index: 1
        operator: "Equal"
        values:
        - "4"          # MAY_READ
      matchActions:
      - action: Sigkill
```

Mount it into the container so the agent picks it up, and the `Sigkill` fires *before* the read returns:

```bash
docker run --name tetragon --rm -d \
    --pid=host --cgroupns=host --privileged \
    -v ${PWD}/file_monitoring_enforce.yaml:/etc/tetragon/tetragon.tp.d/policy.yaml \
    -v /sys/kernel/btf/vmlinux:/var/lib/tetragon/btf \
    quay.io/cilium/tetragon:v1.7.0
```

Drop the `matchActions` block and the same policy becomes observe-only — you get the alert with full lineage but the read proceeds. Swap `TracingPolicyNamespaced` for `TracingPolicy` to apply it cluster-wide on Kubernetes.

**Try next:** Run the observe-only version first (no `matchActions`), `cat /etc/shadow`, and read your own name out of the event's process ancestry in `tetra getevents`. Then add `action: Sigkill`, try the `cat` again, and watch the shell report the process was killed — enforcement that happened in the kernel, not after an alert reached a server somewhere.
