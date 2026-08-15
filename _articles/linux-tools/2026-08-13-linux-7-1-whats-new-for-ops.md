---
title: "What is new in Linux 7.1 for operators"
date: 2026-08-13
track: linux-tools
summary: "Linux 7.1 (released June 2026, now at 7.1.8 stable) ships a rewritten NTFS driver, three clone3() flags that close races in process supervision, and BPF control over the io_uring dispatch loop. What each one is, how to check for it, and what else in the release matters for operations."
reading_time: 6
tags: [kernel, linux-7-1, clone3, ntfs, io-uring]
sources:
  - title: "The first half of the 7.1 merge window — LWN"
    url: "https://lwn.net/Articles/1067250/"
  - title: "The rest of the 7.1 merge window — LWN"
    url: "https://lwn.net/Articles/1067785/"
  - title: "Linux 7.1 — LWN"
    url: "https://lwn.net/Articles/1077814/"
  - title: "Linux 7.1 — Kernel Newbies"
    url: "https://kernelnewbies.org/Linux_7.1"
  - title: "Linux 7.1 Released — Phoronix"
    url: "https://www.phoronix.com/news/Linux-7.1-Released"
---

**Gist.** Process supervision rests on userspace conventions — double-forking to hand reaping to init, `prctl()` calls racing against `execve()` — each of which has a window in which it is wrong, and New Technology File System (NTFS) access and input/output (I/O) dispatch have each been carried by a single hand-rolled in-kernel path. Linux 7.1, released on June 14, 2026 and at 7.1.8 in the stable series as of this writing, replaces several of those arrangements with explicit kernel primitives: three new `clone3()` flags, a second NTFS driver built on the shared `iomap` block-mapping layer, and a struct_ops attachment point that lets a BPF program supply the io_uring dispatch loop. The cost is duplication and churn: two NTFS drivers coexist in-tree with no single documented default, the new flags exist only on 7.1 and later so every caller needs a runtime probe, and the io_uring hook moves policy that was fixed and reviewed into loadable programs.

Identifying the running kernel and the compiled-in options is the precondition for any of the checks below:

```sh
uname -r                       # 7.1.x on the new series
grep -E 'NTFS|IO_URING' /boot/config-$(uname -r)
```

## A rewritten NTFS driver

7.1 merges a complete rewrite of the NTFS driver. It ships **alongside the existing ntfs3 driver as an alternative rather than a replacement**. Such overlap has precedent: the legacy read-only NTFS driver stayed in-tree alongside ntfs3 for several releases before its removal in 6.9. The new implementation has full write support and is built on **iomap**, the block-mapping infrastructure that ext4 and XFS have been converging on.

The structural consequence is what matters operationally. A filesystem that maps file offsets to block extents through iomap shares one buffered-I/O and direct-I/O implementation with every other iomap user, so large-folio support and bug fixes in that layer apply to it without per-filesystem work. A filesystem that hand-rolls its buffered-I/O path receives none of that, and each fix has to be reproduced in every driver that carries its own copy.

Two drivers registered for the same on-disk format means the mount path, not the administrator, picks the implementation unless the type is named:

```sh
grep -i ntfs /proc/filesystems          # registered NTFS drivers
mount | grep -i ntfs                     # which one a mount used
dmesg | grep -i ntfs                     # driver banner at mount time
```

Distributions will differ on which driver answers `-t ntfs` while both are in-tree, so the type belongs pinned explicitly in `fstab` where the choice matters. The ntfs3 driver has a history of slow maintenance; which driver a distribution blesses is worth establishing before production mounts move.

## clone3() learns supervision primitives

Three new `clone3()` flags close races in process management, continuing the pidfd work ([covered here previously](/articles/linux-tools/2026-08-07-pidfd-race-free-process-management)):

- **`CLONE_AUTOREAP`** — the child is reaped automatically on exit. No zombie entry persists and no `wait()` call is required, so a supervisor that only needs the liveness answer no longer needs a `SIGCHLD` handler, and daemons that double-fork purely to hand reaping to init have lost their reason to do so.
- **`CLONE_PIDFD_AUTOKILL`** — the child is killed when the last pidfd referring to it is closed. Because file-descriptor closure is performed by the kernel on process exit, this yields kill-on-drop semantics that survive a supervisor crash: the descriptor table is torn down, the last reference goes away, the child dies. **The pidfd is a reference to the process itself, not to its numeric identifier**, so the sequence carries no process-identifier (PID) reuse race, and it replaces the `PR_SET_PDEATHSIG` arrangement, which is tied to the death of the parent *thread* rather than the parent process, is cleared in the child of a `fork()`, and delivers a signal the child may ignore.
- **`CLONE_NNP`** — sets `no_new_privs` atomically at clone time. Previously the child had to call `prctl(PR_SET_NO_NEW_PRIVS)` itself, in the window between `clone()` and `execve()`; a sandbox launcher relying on that had a convention, whereas the flag makes the property hold for every instruction the child executes.

The failure mode common to all three is a caller that assumes the flag took effect on an older kernel. `clone3()` **rejects unknown flags with `EINVAL`**, which makes the probe cheap but also means a caller that ignores the return value silently gets a child with none of the requested semantics — an unreaped zombie, a child that outlives its supervisor, or a process still able to gain privileges through a setuid binary:

```c
struct clone_args args = {
    .flags = CLONE_PIDFD | CLONE_PIDFD_AUTOKILL | CLONE_AUTOREAP,
    .pidfd = (uintptr_t)&pidfd,
};
pid_t pid = syscall(SYS_clone3, &args, sizeof(args));
/* pid < 0 && errno == EINVAL  ->  pre-7.1 kernel: fall back explicitly */
```

The fallback path has to be written, not assumed: on a pre-7.1 kernel the supervisor still needs its `SIGCHLD` handler and its own kill-on-exit logic.

## BPF control over the io_uring dispatch loop

io_uring gained BPF support through **struct_ops**, the mechanism by which the kernel exposes a table of function pointers as an attachment point that a BPF program can fill in. A BPF program can now replace the main dispatch loop, deciding how submitted operations are executed, which allows userspace to build event loops whose scheduling logic runs kernel-side. This is the pattern `sched_ext` established for CPU schedulers: take a policy that was fixed in the kernel source, expose it as a struct_ops target, and iterate on it without rebuilding or rebooting.

Alongside it, the verifier's stack-liveness tracking was redesigned. LWN reports the change makes verification **"much faster"** for many programs, which bears on load time for large programs using Compile Once — Run Everywhere (CO-RE) relocations. No published figure separates the old and new tracking on a named workload.

This is early infrastructure rather than a deployment candidate, but confirming that builds carry the prerequisites is inexpensive:

```sh
grep -E 'CONFIG_BPF_JIT=|CONFIG_IO_URING=' /boot/config-$(uname -r)
bpftool struct_ops list          # attachment points on a running kernel
```

## Lightning round

Also in 7.1: **Intel Flexible Return and Event Delivery (FRED)** is enabled by default where the hardware supports it, replacing the older interrupt-descriptor-table exception-delivery path; presence shows up as `grep -o fred /proc/cpuinfo | head -1`. Unix-domain sockets accept **`user.*` extended attributes (xattrs)**, so a service can annotate its socket in-band rather than in a side file (`setfattr -n user.svc -v api /run/app.sock`). **UDP-Lite was removed** after years of disuse, so anything setting `IPPROTO_UDPLITE` fails rather than degrades. Network File System (NFS) servers can issue **cryptographically signed file handles** (`sign_fh`), so a forged or guessed handle fails verification rather than reaching the exported filesystem. The multi-generational least-recently-used reclaim implementation (MGLRU) gained batched young-flag checking, reported as a measurable reclaim improvement on some workloads; the Data Access MONitor (DAMON) can run multiple tuning algorithms concurrently; and `sched_ext` gained groundwork for **sub-schedulers**, which points toward per-control-group scheduling policies in later releases.

The through-line of 7.1 is not a single feature but a substitution: zombie reaping, privilege dropping and dispatch policy move out of racy userspace convention and into kernel primitives whose guarantees hold at the instruction boundary.

## Pitfalls

- **A mount succeeds but lands on the other NTFS driver.** Two drivers register for the same format, so `-t ntfs` resolves according to distribution configuration; the symptom is a behaviour or performance difference that follows a kernel upgrade with no `fstab` change.
- **`clone3()` returns `EINVAL` and the caller treats it as a transient fork failure.** The flags are 7.1-only, so on an older kernel every child is created with the legacy semantics or not at all, depending on how the return is handled.
- **`CLONE_PIDFD_AUTOKILL` is defeated by a leaked pidfd.** The child dies when the *last* reference closes, so a descriptor duplicated into an unrelated long-lived process keeps the child alive past the supervisor's exit.
- **`CLONE_AUTOREAP` removes the exit status along with the zombie.** A supervisor that needs the child's exit code cannot obtain it through `wait()` if the kernel has already reaped the child.
- **Assuming `CLONE_NNP` is equivalent to a sandbox.** It sets `no_new_privs` only; the child retains whatever capabilities, namespaces and filesystem access it was given.
- **Testing FRED by CPU model rather than by flag.** It is enabled where hardware supports it, so the `fred` flag in `/proc/cpuinfo`, not the processor generation, is the observable fact.
- **Assuming `bpftool struct_ops list` proves io_uring BPF support.** The command lists attachment points present on the running kernel; an empty or unexpected listing reflects the build configuration rather than a runtime fault.
