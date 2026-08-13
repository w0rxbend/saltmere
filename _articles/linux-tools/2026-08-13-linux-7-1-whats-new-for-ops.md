---
title: "What's new in Linux 7.1 for operators"
date: 2026-08-13
track: linux-tools
summary: "Linux 7.1 (released June 2026, now at 7.1.8 stable) ships a rewritten NTFS driver, three clone3() flags that make process supervision race-free, and BPF control over the io_uring dispatch loop. What each one is, how to check for it, and what else in the release matters for ops."
reading_time: 5
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

Linux 7.1 landed on June 14, 2026 — half a day early, because Linus was ahead of his home time zone — and the stable series is at 7.1.8 as of this writing, with 7.2 sitting at rc7. The headlines went to Intel FRED being enabled by default and a brand-new NTFS driver, but the release is quietly full of operator-facing changes. Here are the three I'd actually go try, plus a lightning round.

First, know what you're running:

```sh
uname -r                       # 7.1.x if you're on the new series
grep -E 'NTFS|IO_URING' /boot/config-$(uname -r)
```

## A rewritten NTFS driver

7.1 merges a complete rewrite of the NTFS filesystem driver, shipping alongside the existing ntfs3 as an alternative rather than a replacement — the same overlap strategy the kernel used when ntfs3 displaced the old read-mostly driver. The new implementation has full write support and, more interestingly for long-term health, is built on **iomap**, the modern block-mapping infrastructure that ext4 and XFS have been converging on. That matters because iomap-based filesystems get large-folio support, cleaner direct I/O, and shared bug fixes for free, instead of each filesystem hand-rolling its buffered-I/O path.

If you export USB disks to Windows machines, dual-boot, or mount NTFS images in data-recovery workflows, this is the one to test. Check what your kernel offers and which driver actually mounted:

```sh
grep -i ntfs /proc/filesystems          # registered NTFS drivers
mount | grep -i ntfs                     # which one a mount used
dmesg | grep -i ntfs                     # driver banner at mount time
```

Distributions will differ on which driver is the default `-t ntfs` handler while both are in-tree, so pin the type explicitly in fstab if you care. The ntfs3 driver has a history of slow maintenance; watch which one your distro blesses before moving production mounts.

## clone3() learns supervision tricks

Three new `clone3()` flags close long-standing races in process management, continuing the pidfd story ([covered here previously](/articles/linux-tools/2026-08-07-pidfd-race-free-process-management)):

- **CLONE_AUTOREAP** — the child is reaped automatically on exit; no zombie, no `wait()` required. Every daemon that double-forks or every supervisor that only cares about "still running?" can stop writing SIGCHLD handlers.
- **CLONE_PIDFD_AUTOKILL** — the child is killed when the last pidfd referring to it is closed. This gives you kill-on-drop semantics: if the supervisor crashes, its children die with it, with no PID-reuse race and no PR_SET_PDEATHSIG contortions.
- **CLONE_NNP** — sets `no_new_privs` atomically at clone time, instead of the child racing to call `prctl(PR_SET_NO_NEW_PRIVS)` before `execve()`. Sandbox launchers get a guarantee where they previously had a convention.

Probing for support is the usual clone3 dance — the syscall rejects unknown flags with `EINVAL`, so try it on a scratch child:

```c
struct clone_args args = {
    .flags = CLONE_PIDFD | CLONE_PIDFD_AUTOKILL | CLONE_AUTOREAP,
    .pidfd = (uintptr_t)&pidfd,
};
pid_t pid = syscall(SYS_clone3, &args, sizeof(args));
/* pid < 0 && errno == EINVAL  ->  pre-7.1 kernel */
```

Expect container runtimes and service managers to grow flags for these over the next cycles; the primitives finally match what supervisors always wanted to express.

## BPF takes over the io_uring dispatch loop

io_uring gained BPF support via struct_ops: a BPF program can now replace the main dispatch loop, deciding how submitted operations are executed and letting userspace build custom event loops with kernel-side logic. It's the same pattern sched_ext used for schedulers — take a fixed in-kernel policy, expose it as a struct_ops attachment point, iterate in BPF without rebooting. Alongside it, the verifier's stack-liveness tracking was redesigned, which LWN notes makes verification "much faster" for many programs — good news if you've watched large CO-RE programs take seconds to load.

This is early-days infrastructure rather than something to deploy this quarter, but it's worth confirming your builds carry it before you need it:

```sh
grep -E 'CONFIG_BPF_JIT=|CONFIG_IO_URING=' /boot/config-$(uname -r)
bpftool struct_ops list          # attachment points on a running kernel
```

## Lightning round

Also in 7.1, briefly: **Intel FRED** is now on by default where hardware supports it (Panther Lake onward) — faster, saner exception delivery; check `grep -o fred /proc/cpuinfo | head -1`. Unix-domain sockets accept **user.\* xattrs**, so a service can finally document its socket in-band (`setfattr -n user.svc -v api /run/app.sock`). **UDP-Lite was removed** after years of disuse — audit anything ancient that sets `IPPROTO_UDPLITE`. NFS servers can hand out **cryptographically signed file handles** (`sign_fh`) to defeat handle-guessing. MGLRU picked up batched young-flag checking with 60%+ gains on some workloads, DAMON can now run multiple tuning algorithms concurrently, and sched_ext grew groundwork for **sub-schedulers**, pointing at per-cgroup scheduling policies in future releases.

The through-line of 7.1: not one big feature, but a kernel steadily replacing racy userspace conventions — zombie reaping, privilege dropping, dispatch loops — with explicit, race-free kernel primitives.

**Try next:** boot 7.1.8 in a VM, write the ten-line clone3 probe above, and confirm that closing the pidfd with CLONE_PIDFD_AUTOKILL actually takes the child down — then compare with the PR_SET_PDEATHSIG dance it replaces.
