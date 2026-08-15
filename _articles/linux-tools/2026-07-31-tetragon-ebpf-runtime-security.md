---
title: "Tetragon: eBPF Runtime Security That Filters and Enforces In-Kernel"
date: 2026-07-31
track: linux-tools
summary: "Tetragon hooks the kernel with eBPF to observe process lineage, file access, and syscalls. Unlike alert-only tools it filters and enforces (SIGKILL, return-value override) in-kernel, with no userspace round-trip. Covers the architecture, a standalone Docker run, and a TracingPolicy that terminates a process reading /etc/shadow."
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

**Gist.** Conventional runtime-security agents observe the kernel with extended Berkeley Packet Filter (eBPF) programs, ship raw events to a userspace rule engine, and respond after the fact with an alert — by which time the read, the exec or the connect has already completed. Tetragon moves both the match and the response into the eBPF program itself: a policy encodes its predicates into the attached program, so non-matching events are discarded in-kernel, and a matching one can carry an action such as `Sigkill` or `Override` that executes synchronously inside the hook. The cost is that the policy surface is the kernel's own internal application binary interface — kprobe names, argument indices and raw constants such as `MAY_READ` — rather than a portable, semantically stable rule language.

Tetragon originates with Isovalent (the Cilium and eBPF team, now part of Cisco) and ships under Cilium, a **CNCF Graduated** project since October 2023. Tetragon reached **1.0 in November 2023** and has continued on a 1.x line since; the release notes on GitHub are the authority for what any given version adds.

## Attachment points and the event path

eBPF programs attach to **kprobes** (arbitrary kernel functions), syscalls, tracepoints, uprobes, and **Linux Security Module (LSM) hooks**. When a hook fires, the event travels kernel-to-userspace over a ring buffer to the Tetragon agent, which enriches it with process context: the executable, its arguments, and the **entire ancestry** — the process, its parent chain, and the container and pod it belongs to, resolved through cgroups. The unit of observation is therefore not "a file was opened" but the exec chain that reached that open.

The attachment point determines what is observable and what enforcement means. A **kprobe on a syscall entry sees the arguments as userspace supplied them**, which is the classic time-of-check-to-time-of-use exposure: a pointer argument can be rewritten by another thread between the check and the kernel's own dereference. An **LSM hook such as `security_file_permission` runs after the kernel has resolved the arguments into internal objects** — a `struct file` rather than a user-supplied path string — so a path predicate evaluated there is matched against what the kernel resolved, not against what the caller wrote.

## Where the filtering happens

The load-bearing property is that **selectors are compiled into the eBPF program rather than evaluated in userspace**. A policy interested only in reads of a few paths encodes those predicates in-kernel; events that fail them are dropped at the hook and never pay for the ring-buffer write, the wake-up, or the userspace parse. Enforcement follows the same path: a matched selector may carry `Sigkill`, terminating the offending process, or `Override`, forcing the hooked function to return a chosen error value. **No userspace round-trip participates in either decision**, so there is no window in which the operation proceeds while a decision is pending.

This is the concrete difference from **Falco**, the other widely deployed eBPF security tool. Falco captures syscalls with eBPF but evaluates its rules in *userspace* and, by design, detects rather than blocks: it emits an alert and leaves any blocking to external tooling. Falco carries the larger community rule ecosystem; Tetragon's distinguishing capabilities are in-kernel filtering, in-kernel enforcement, and process lineage. The two are frequently run together.

## Running the agent standalone

Kubernetes is not required. As a plain container the agent needs the host process-ID and cgroup namespaces — without them the cgroup identifiers it reads cannot be mapped to host processes — and the kernel's BPF Type Format (BTF) blob, which is what makes compile-once-run-everywhere (CO-RE) relocation of struct offsets possible on the running kernel:

```bash
docker run --name tetragon --rm -d \
    --pid=host --cgroupns=host --privileged \
    -v /sys/kernel/btf/vmlinux:/var/lib/tetragon/btf \
    quay.io/cilium/tetragon:$TETRAGON_VERSION
```

`TETRAGON_VERSION` stands for a released tag from the project's GitHub releases page; the image is published per release rather than under a rolling name.

The live event stream, including lineage, is read with:

```bash
docker exec -ti tetragon tetra getevents -o compact
```

Without a policy loaded the agent reports exec and exit events across the host. Everything narrower is policy.

## A policy that filters and enforces

A **TracingPolicy** declares three things: what to hook, what to match, and what to do on a match. The policy below attaches a kprobe to the LSM function `security_file_permission` — the `kprobes` block, not Tetragon's separate BPF-LSM attachment — narrows in-kernel to three path prefixes combined with read access, and terminates the caller.

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

Three details are load-bearing. `syscall: false` states that the hooked symbol is an internal kernel function, not a syscall entry, which changes how arguments are read. **The `args` block is a type declaration, not a filter**: it tells the BPF program how to interpret argument slot 0 as a `struct file` and slot 1 as an integer, and `matchArgs` then applies operators to those typed values. **Multiple `matchArgs` entries within one selector are conjunctive** — the path prefix *and* the mask value must both hold — while the `values` list inside a single entry is disjunctive.

The mask literal `4` is `MAY_READ` as the kernel defines it. It is a bitmask, and `Equal` compares the whole integer, so a caller requesting read together with another bit produces a different value and does not match.

Mounting the file into the agent's policy directory loads it:

```bash
docker run --name tetragon --rm -d \
    --pid=host --cgroupns=host --privileged \
    -v ${PWD}/file_monitoring_enforce.yaml:/etc/tetragon/tetragon.tp.d/policy.yaml \
    -v /sys/kernel/btf/vmlinux:/var/lib/tetragon/btf \
    quay.io/cilium/tetragon:$TETRAGON_VERSION
```

Removing the `matchActions` block leaves the identical match logic as observe-only: the event is reported with full ancestry and the read proceeds. Substituting `TracingPolicy` for `TracingPolicyNamespaced` applies the policy cluster-wide on Kubernetes.

A useful progression is to load the observe-only variant, run `cat /etc/shadow`, and read the exec ancestry out of `tetra getevents`; then add `action: Sigkill` and repeat, observing the shell report the process as killed rather than an alert arriving after the fact.

## Pitfalls

- **A kprobe on a syscall entry point matched against a user-supplied path can be defeated by rewriting that memory after the check.** The pointer is under the caller's control until the kernel dereferences it; matching at an LSM hook, where arguments are already resolved kernel objects, removes that window.
- **`Equal` on the access mask is an integer comparison, not a bit test.** A request combining read with another permission bit yields a value other than `4` and silently fails to match, so the policy neither reports nor enforces.
- **`Prefix` matches a string prefix, not a directory boundary.** A value of `/etc/shadow` also matches a path such as `/etc/shadowfile`, and `/root/.ssh` matches sibling names sharing that prefix.
- **A missing or mismatched BTF blob prevents CO-RE relocation and the programs fail to load**, leaving an agent that starts but reports no events; the mount of `/sys/kernel/btf/vmlinux` must come from the running kernel.
- **Omitting `--pid=host` or `--cgroupns=host` degrades enrichment rather than producing an obvious error**: the agent runs, but process ancestry and container attribution are computed against the container's own namespaces.
- **`Sigkill` terminates the process, not the operation in isolation.** A policy hooking a function reached by a long-lived supervisor or an init process kills that process with whatever consequences follow for its children.
- **A `TracingPolicyNamespaced` object applies only within its namespace.** Loading it and expecting cluster-wide coverage leaves workloads in other namespaces unhooked with no warning.
