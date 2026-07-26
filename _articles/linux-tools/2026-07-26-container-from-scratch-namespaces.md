---
title: "Containers from scratch: a process in a costume of namespaces"
date: 2026-07-26
track: linux-tools
summary: "A container isn't a thing the kernel knows about — it's an ordinary process wearing namespaces and cgroup limits. Build one by hand with unshare, a rootfs, and the cgroup v2 filesystem."
reading_time: 5
tags: [namespaces, cgroups, containers, unshare, linux, pid-namespace, rootfs]
sources:
  - title: "unshare(1) — Linux manual page"
    url: "https://man7.org/linux/man-pages/man1/unshare.1.html"
  - title: "namespaces(7) — Linux manual page"
    url: "https://man7.org/linux/man-pages/man7/namespaces.7.html"
  - title: "user_namespaces(7) — Linux manual page"
    url: "https://man7.org/linux/man-pages/man7/user_namespaces.7.html"
  - title: "Control Group v2 — The Linux Kernel documentation"
    url: "https://docs.kernel.org/admin-guide/cgroup-v2.html"
  - title: "Liz Rice — containers-from-scratch"
    url: "https://github.com/lizrice/containers-from-scratch"
---

There is no `container` object in the Linux kernel. There is no syscall that makes one. A container is a normal process that has been lied to about the system it runs on — given its own hostname, its own PID 1, its own root filesystem, its own view of the network — and then fenced in with resource limits. Two kernel features do all the work: **namespaces** isolate *what a process can see*, and **cgroups** limit *what a process can use*.

The fastest way to believe this is to build one with shell commands. Everything below runs on any modern Linux with a 5.x+ kernel and cgroups v2 (the default since roughly 2021). You'll want `root` for the first pass; a rootless variant is at the end.

## Step 1: New namespaces with unshare

`unshare` runs a program in fresh namespaces. The kernel gives every process a set of namespaces; these flags detach the child into new ones (`unshare(1)`):

```sh
sudo unshare --uts --pid --mount --net --fork --mount-proc bash
```

You are now in a new shell. Watch what changed:

```sh
hostname container01        # --uts: rename the host, host system is unaffected
echo $$                     # prints 1 — this bash is PID 1 in its namespace
ps aux                      # only sees processes in the new PID namespace
ip link                     # --net: just a lonely, down 'lo' interface
```

Two flags are load-bearing here. `--fork` is required with `--pid`: the calling process can't itself enter a new PID namespace (its PID is already fixed), so `unshare` forks a child that becomes PID 1. `--mount-proc` remounts `/proc` inside a new mount namespace so `ps` reflects the *new* PID namespace instead of the host's — without it, `/proc` would still show every process on the machine.

| Flag | Namespace | What it isolates |
|------|-----------|------------------|
| `--uts` | UTS | hostname & domain name |
| `--pid` | PID | process IDs (your shell becomes PID 1) |
| `--mount` | mount | the filesystem mount table |
| `--net` | network | interfaces, routes, iptables |
| `--user` | user | UID/GID mappings (enables rootless) |
| `--ipc` | IPC | System V IPC, POSIX message queues |

## Step 2: Give it a root filesystem

A hostname isn't much of a container while the process still sees the host's `/`. Swap in a minimal userland with `chroot`, done inside the mount namespace so the host is untouched. Grab an Alpine minirootfs — a few MB of a working `/`:

```sh
mkdir -p /tmp/rootfs
curl -sSL https://dl-cdn.alpinelinux.org/alpine/v3.20/releases/x86_64/alpine-minirootfs-3.20.0-x86_64.tar.gz \
  | tar -xz -C /tmp/rootfs
```

Now, from inside the namespaced shell from Step 1 (drop `--mount-proc` this time so we mount `/proc` ourselves):

```sh
sudo unshare --uts --pid --mount --net --fork bash
hostname container01
chroot /tmp/rootfs /bin/sh          # pivot into the new root
mount -t proc proc /proc            # a fresh procfs for the new PID namespace
cat /etc/os-release                 # "Alpine Linux" — a different distro than the host
ps aux                              # PID 1 is /bin/sh, and almost nothing else
```

That's a container: PID 1, its own hostname, its own root filesystem, its own network stack. Docker's runtime does a more careful job — it uses `pivot_root` instead of `chroot`, marks mounts private, and drops capabilities — but the shape is exactly this.

## Step 3: Fence it in with cgroups v2

Namespaces hide the rest of the system from the process. They do *not* stop it from eating all the RAM or pinning every core. That's the cgroup's job. In cgroups v2 there is a single unified hierarchy mounted at `/sys/fs/cgroup`, and you drive it entirely by reading and writing files (kernel `cgroup-v2` docs).

First, enable the controllers you want to hand down to child groups by writing to `cgroup.subtree_control` — a space-separated list where `+`/`-` turns controllers on and off:

```sh
cd /sys/fs/cgroup
echo "+cpu +memory" > cgroup.subtree_control
```

Create a group for our container and set limits. `memory.max` is a byte count (default `max`); `cpu.max` is `"$MAX $PERIOD"` in microseconds, so `50000 100000` means 50 ms of CPU per 100 ms — half of one core:

```sh
mkdir /sys/fs/cgroup/demo
echo $((100 * 1024 * 1024)) > /sys/fs/cgroup/demo/memory.max   # 100 MiB hard cap
echo "50000 100000"         > /sys/fs/cgroup/demo/cpu.max       # 0.5 CPU
```

Move a process in by writing its PID to `cgroup.procs`. Do this to the shell that's running your container, then prove the CPU cap holds:

```sh
echo $$ > /sys/fs/cgroup/demo/cgroup.procs   # this shell + its children now constrained
yes > /dev/null &                            # a busy loop
top -p $!                                     # sits at ~50% of one CPU, not 100%
cat /sys/fs/cgroup/demo/memory.current        # live memory usage of the group, in bytes
kill %1
```

The process still *thinks* it has the whole machine — `nproc` inside the container may report every core — but the scheduler will not let it exceed the slice you granted. Isolation of the *view* (namespaces) and limits on *usage* (cgroups) are deliberately separate mechanisms; a container is just the combination.

## What the runtime actually calls

`unshare(1)` is a thin wrapper over the `unshare(2)` and `clone(2)` syscalls. A container runtime skips the CLI and passes flags to `clone()` directly when it forks the container's first process (`namespaces(7)`):

| `clone()` flag | `unshare` equivalent |
|----------------|----------------------|
| `CLONE_NEWUTS` | `--uts` |
| `CLONE_NEWPID` | `--pid` |
| `CLONE_NEWNS`  | `--mount` |
| `CLONE_NEWNET` | `--net` |
| `CLONE_NEWUSER`| `--user` |
| `CLONE_NEWIPC` | `--ipc` |

To do all of this without `sudo`, add a **user namespace**, which maps your unprivileged UID to `root` *inside* the container — the basis of rootless containers (`user_namespaces(7)`):

```sh
unshare --user --map-root-user --uts --pid --mount --fork bash
id      # uid=0(root) inside — but powerless outside the namespace
```

**Try next:** wire your container into the network. Create a `veth` pair, move one end into the container's net namespace (`ip link set veth1 netns <pid>`), assign addresses to both ends, and ping the host from inside — reproducing, by hand, what a bridge network does under Docker.
