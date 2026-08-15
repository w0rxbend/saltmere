---
title: "Containers from scratch: a process in a costume of namespaces"
date: 2026-07-26
track: linux-tools
summary: "A container is not an object the kernel knows about — it is an ordinary process wearing namespaces and cgroup limits. Constructing one by hand with unshare, a rootfs, and the cgroup v2 filesystem."
reading_time: 6
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

**Gist.** The Linux kernel exposes no `container` object and no syscall that creates one; a container is an ordinary process whose view of the system has been narrowed and whose resource consumption has been capped. Two independent mechanisms do the work: **namespaces isolate what a process can see** (hostname, process identifiers, mounts, network interfaces, inter-process communication objects, user identifiers), and **control groups (cgroups) limit what a process can use** (memory, central processing unit (CPU) time). The cost of that separation is that the two mechanisms do not know about each other — a process can be fenced by a cgroup while still reading the host's `/proc`, and it can be fully namespaced while consuming every core on the machine.

The construction below uses shell commands only. It requires a kernel with cgroups v2 — merged in Linux 4.5 — mounted as the unified hierarchy, and `root` for the first pass; a rootless variant using a user namespace appears at the end.

## Step 1: new namespaces with unshare

Every process holds a set of namespace memberships. `unshare(1)` detaches a child into fresh ones and executes a program there:

```sh
sudo unshare --uts --pid --mount --net --fork --mount-proc bash
```

The resulting shell observes a different system:

```sh
hostname container01        # --uts: renames only inside the namespace; host unaffected
echo $$                     # prints 1 — this bash is PID 1 in its namespace
ps aux                      # only processes in the new PID namespace
ip link                     # --net: a single 'lo' interface, in state DOWN
```

Two flags are load-bearing. **`--fork` is required alongside `--pid`**: a process identifier (PID) is fixed at creation, so the calling process cannot itself move into a new PID namespace; `unshare` forks a child, and that child becomes PID 1 of the new namespace. **`--mount-proc` remounts `/proc` inside the new mount namespace**, so that `ps` — which reads `/proc` — reflects the new PID namespace. Without it, `/proc` remains the host's procfs instance and every process on the machine is still listed, even though the shell's own PID is 1.

| Flag | Namespace | Isolated resource |
|------|-----------|------------------|
| `--uts` | UTS | hostname and domain name |
| `--pid` | PID | process identifiers (the shell becomes PID 1) |
| `--mount` | mount | the filesystem mount table |
| `--net` | network | interfaces, routes, iptables rules |
| `--user` | user | user and group identifier mappings (enables rootless operation) |
| `--ipc` | IPC | System V inter-process communication, POSIX message queues |

## Step 2: a private root filesystem

A renamed host is not yet a container while the process still resolves `/` to the host's root. `chroot` substitutes a minimal userland, performed inside the mount namespace so that the host mount table is untouched. An Alpine minirootfs supplies a working `/` in a few megabytes:

```sh
mkdir -p /tmp/rootfs
curl -sSL https://dl-cdn.alpinelinux.org/alpine/v3.20/releases/x86_64/alpine-minirootfs-3.20.0-x86_64.tar.gz \
  | tar -xz -C /tmp/rootfs
```

From inside a namespaced shell, this time without `--mount-proc` so that procfs is mounted explicitly after the root change:

```sh
sudo unshare --uts --pid --mount --net --fork bash
hostname container01
chroot /tmp/rootfs /bin/sh          # change the root directory
mount -t proc proc /proc            # a fresh procfs for the new PID namespace
cat /etc/os-release                 # "Alpine Linux" — a distribution unlike the host's
ps aux                              # PID 1 is /bin/sh, and little else
```

The ordering is the invariant: **`/proc` must be mounted after the root change, and inside the mount namespace**, because procfs materialises the PID namespace of the process that performs the mount. Mounting it before `chroot` places it in the outer root; mounting it outside the mount namespace makes the change visible to the host.

That composition — PID 1, a private hostname, a private root filesystem, a private network stack — is a container. A production runtime performs the same steps more carefully: it uses `pivot_root` rather than `chroot`, marks mounts private so that propagation does not leak into the host, and drops capabilities from the container's first process.

## Step 3: bounding consumption with cgroups v2

Namespaces conceal the rest of the system; they place no bound on memory or CPU consumption. That is the cgroup's role. cgroups v2 presents **a single unified hierarchy mounted at `/sys/fs/cgroup`, driven entirely through file reads and writes** (kernel `cgroup-v2` documentation).

Controllers are made available to child groups by writing to `cgroup.subtree_control`, a space-separated list in which `+` and `-` enable and disable a controller:

```sh
cd /sys/fs/cgroup
echo "+cpu +memory" > cgroup.subtree_control
```

A group is a directory; its limits are files inside it. `memory.max` holds a byte count and defaults to the literal `max`. `cpu.max` holds `"$MAX $PERIOD"` in microseconds, so `50000 100000` grants 50 ms of CPU time per 100 ms period — half of one core:

```sh
mkdir /sys/fs/cgroup/demo
echo $((100 * 1024 * 1024)) > /sys/fs/cgroup/demo/memory.max   # 100 MiB hard cap
echo "50000 100000"         > /sys/fs/cgroup/demo/cpu.max       # 0.5 CPU
```

Membership is set by writing a PID into `cgroup.procs`. Applying this to the shell that runs the container places that shell and its descendants under the limits:

```sh
echo $$ > /sys/fs/cgroup/demo/cgroup.procs   # this shell and its children are constrained
yes > /dev/null &                            # a busy loop
top -p $!                                     # approximately 50% of one CPU, not 100%
cat /sys/fs/cgroup/demo/memory.current        # current usage of the group, in bytes
kill %1
```

The constrained process retains an unconstrained *view*: `nproc` inside the container may report every core on the host, because the CPU count is read from the host's topology and no namespace virtualises it. The scheduler nevertheless refuses to grant more than the configured slice. **Isolation of the view and limitation of usage are separate mechanisms**, and a container is their combination.

## The syscall interface beneath the tool

`unshare(1)` is a thin wrapper over the `unshare(2)` system call. The same namespace flags are accepted by `clone(2)`, and a container runtime bypasses the command line by passing them to `clone()` when it creates the container's first process (`namespaces(7)`):

| `clone()` flag | `unshare` equivalent |
|----------------|----------------------|
| `CLONE_NEWUTS` | `--uts` |
| `CLONE_NEWPID` | `--pid` |
| `CLONE_NEWNS`  | `--mount` |
| `CLONE_NEWNET` | `--net` |
| `CLONE_NEWUSER`| `--user` |
| `CLONE_NEWIPC` | `--ipc` |

Performing the whole construction without `sudo` requires a **user namespace**, which maps an unprivileged user identifier (UID) to `root` *inside* the namespace — the basis of rootless containers (`user_namespaces(7)`):

```sh
unshare --user --map-root-user --uts --pid --mount --fork bash
id      # uid=0(root) inside the namespace, unprivileged outside it
```

A further exercise connects the container to a network: create a `veth` pair, move one end into the container's network namespace with `ip link set veth1 netns <pid>`, assign an address to each end, and reach the host from inside — reproducing by hand what a bridge network provides under a container runtime.

## Pitfalls

- **`ps` lists every host process despite the shell reporting PID 1.** `/proc` was not remounted inside the new mount namespace; the procfs instance still belongs to the host's PID namespace. `--mount-proc`, or an explicit `mount -t proc proc /proc` after the root change, is required.
- **`unshare --pid` without `--fork` leaves the shell outside the new PID namespace.** The calling process keeps its existing PID; only children created afterwards enter the namespace.
- **Writing to `memory.max` or `cpu.max` in a group whose parent has not enabled the controller fails.** The controller must first be delegated through the parent's `cgroup.subtree_control`.
- **`nproc` and other host-topology readings inside the container ignore `cpu.max`.** No namespace virtualises the CPU count, so runtimes that size thread pools from `nproc` will oversubscribe a fractional CPU quota.
- **`chroot` changes the root directory but does not detach mount propagation.** Without a mount namespace and private propagation, mounts performed inside the new root can become visible on the host.
- **The container's network namespace starts with only a down loopback interface.** Any outbound traffic fails until a `veth` pair or equivalent device is created and brought up.
