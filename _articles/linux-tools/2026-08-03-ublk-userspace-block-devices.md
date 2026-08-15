---
title: "ublk: serving a block device from userspace over io_uring"
date: 2026-08-03
track: linux-tools
summary: "A block device that appears in lsblk but whose reads and writes are handled by an ordinary userspace process. ublk_drv, merged in Linux 6.0, uses io_uring command passthrough to hand each I/O request to a daemon and collect the result, avoiding NBD's per-request socket round trip and TCMU's SCSI target stack."
reading_time: 7
tags: [linux, ublk, io-uring, block-devices, ublksrv, kernel]
sources:
  - title: "Userspace block device driver (ublk driver) — kernel.org docs"
    url: "https://docs.kernel.org/block/ublk.html"
  - title: "An io_uring-based user-space block driver (LWN.net)"
    url: "https://lwn.net/Articles/903855/"
  - title: "ublk-org/ublksrv — userspace block device driver"
    url: "https://github.com/ublk-org/ublksrv"
  - title: "Linux 6.0 — Kernel Newbies changelog"
    url: "https://kernelnewbies.org/Linux_6.0"
  - title: "Documentation/block/ublk.rst (torvalds/linux)"
    url: "https://github.com/torvalds/linux/blob/master/Documentation/block/ublk.rst"
---

**Gist.** Block-device logic such as qcow2 image parsing, encryption, deduplication or network-backed storage is expensive to implement in the kernel and dangerous to get wrong there, but the older escape hatches — Network Block Device (NBD) and the SCSI target userspace passthrough (TCMU) — each impose a transport cost on every request. ublk, whose kernel component `ublk_drv` was merged in **Linux 6.0** (October 2022), exposes a normal `blk-mq` block device whose requests are delivered to a userspace daemon as **io_uring passthrough commands** (`IORING_OP_URING_CMD`), one outstanding command per request tag. The cost is that device availability now depends on a userspace process: if the daemon dies, in-flight requests stall until a replacement re-attaches, and the original 6.0 design copies request data between kernel pages and the daemon's buffer rather than mapping it.

## Two device nodes, two planes

ublk separates a control plane from a data plane, and the separation is visible as distinct device nodes.

A single global control node, `/dev/ublk-control`, receives management commands. The daemon issues them through io_uring rather than through `ioctl`: `UBLK_CMD_ADD_DEV` (carrying the queue count and per-queue depth), `UBLK_CMD_START_DEV`, and the corresponding stop and delete commands. **Control and data therefore share one submission mechanism**, io_uring command passthrough.

Each added device produces two further nodes:

- `/dev/ublkc0` — a **character** device, the private channel between the kernel driver and the single daemon that owns the device. The I/O descriptor array is `mmap`'d through it.
- `/dev/ublkb0` — the **block** device seen by the rest of the system. It is backed by a request-based `blk-mq` driver, so the block layer's multiqueue support and I/O schedulers apply unchanged; `mkfs` and `mount` treat it as an ordinary disk.

## The FETCH / COMMIT loop

The data plane is one loop, and it carries the whole design. Per the kernel documentation, each hardware queue is served by a daemon thread owning its own io_uring instance. Every I/O request carries a **tag that is unique within its queue**, and the invariant the daemon must maintain is: **for each tag, exactly one io_uring command is outstanding in the kernel at all times**. The command names below are the `UBLK_U_`-prefixed forms used by the current documentation; the original merge used unprefixed names for the same operations.

1. The daemon submits `UBLK_U_IO_FETCH_REQ` for a tag — documented as "sent from the server I/O pthread for fetching future incoming I/O requests destined to `/dev/ublkb*`". The command does not complete; it parks in the kernel as the slot reserved for that tag.
2. A request arrives at `/dev/ublkb0`. `blk-mq` constructs it, the driver fills in the shared `ublksrv_io_desc` entry for the tag (operation, starting sector, length) in the region mapped through `/dev/ublkc0`, and **completes the parked FETCH command**. The completion queue entry (CQE) delivered to the daemon *is* the I/O notification; the payload describing the work is read from the mapped descriptor, not from the CQE.
3. The daemon performs the work: `pread`/`pwrite` against a backing file for a loop target, a network exchange for a network target, nothing at all for the null target.
4. The daemon issues `UBLK_U_IO_COMMIT_AND_FETCH_REQ`, which **commits the result of the finished tag and re-arms the same tag's fetch in a single command**, restoring the invariant without a gap.

Steady state is therefore a stream of `COMMIT_AND_FETCH` commands: no `ioctl`, no socket `send`/`recv`, and no syscall per I/O beyond io_uring's ordinary batched submission and completion. Data is copied between the request's pages and the daemon's buffer; zero-copy arrived as a separate, later feature, not as part of the 6.0 design.

Because the fetch for a tag is re-armed by the same command that reports the previous result, a daemon that returns a completion without re-arming leaves that tag with no parked command, and subsequent requests assigned to it have nowhere to be delivered.

## Recovery when the daemon dies

The device's availability is bound to a userspace process, so ublk defines a recovery path rather than leaving the device to fail. With the `UBLK_F_USER_RECOVERY` flag set at device creation, **a replacement daemon can re-attach to the existing device and re-fetch the outstanding tags**, so requests that were in flight when the previous daemon exited are serviced instead of discarded. Crash recovery is one of the areas where NBD's behaviour has historically been weak.

## Driving it with the `ublk` command-line tool

The reference userspace is the [ublksrv](https://github.com/ublk-org/ublksrv) project, originally Ming Lei's `ubdsrv`. It builds a daemon together with an `ublk` control binary. It requires `liburing >= 2.2` and a 6.0 or later kernel built with `CONFIG_BLK_DEV_UBLK`.

```console
# load the driver
$ sudo modprobe ublk_drv

# null target: discards writes and returns zeroes, so it measures the
# FETCH/COMMIT plumbing rather than any backing store
$ sudo ublk add -t null
dev id 0: nr_hw_queues 1 queue_depth 128 block size 512 dev_capacity ...

# loop target: a device backed by an image file
$ truncate -s 1G ublk-loop.img
$ sudo ublk add -t loop -f ublk-loop.img
dev id 1: nr_hw_queues 1 queue_depth 128 ...

$ lsblk
NAME    MAJ:MIN RM  SIZE RO TYPE MOUNTPOINTS
ublkb0  259:0    0    0B  0 disk
ublkb1  259:1    0    1G  0 disk

# an ordinary block device from this point on
$ sudo mkfs.xfs /dev/ublkb1
$ sudo mount /dev/ublkb1 /mnt

$ ublk list -v          # queues, tags, owning daemon pid
$ ublk del -n 1         # remove one device
$ ublk del -a           # remove all devices
```

Two numbers in the `add` output are the ones that bound throughput. `nr_hw_queues` fixes how many daemon threads, and therefore how many independent io_uring instances, serve the device; `queue_depth` fixes how many tags exist per queue, and thus the maximum number of requests that can be outstanding in userspace at once. Both are set at `UBLK_CMD_ADD_DEV` time.

Measuring the transport in isolation means driving the null target with a queue depth that keeps the loop saturated:

```console
$ sudo ublk add -t null
$ sudo fio --name=t --filename=/dev/ublkb0 --rw=randread \
    --bs=4k --iodepth=64 --ioengine=io_uring
$ ublk list -v          # per-tag io_uring commands in flight, from another shell
```

## Position relative to NBD, TCMU and FUSE

All four move device or filesystem semantics into userspace; they differ in the transport and in how they scale.

- **NBD** carries requests over a socket. It works over a network and on old kernels, but **each I/O is a socket round trip**, and its multiqueue and reconnect behaviour have long been rough. Ming Lei's early measurements, reported in the LWN coverage, showed a ublk-based qcow2 target outperforming `qemu-nbd`; the margin depends heavily on the workload and no independent benchmark reproduces it.
- **TCMU** exposes userspace targets through the SCSI target (LIO) stack, which adds SCSI command processing and passes commands through **a single shared ring per device**, so it does not scale across hardware queues. ublk gives each queue its own io_uring instance and its own daemon thread.
- **FUSE** (Filesystem in Userspace) is the nearest conceptual relative but presents a *filesystem* interface rather than a block one, and uses the FUSE protocol rather than io_uring.

`ublk_drv` is a small driver because it delegates every device-specific decision to the daemon; what it contributes is the tag-indexed FETCH/COMMIT channel and the `blk-mq` front end.

## Pitfalls

- **A daemon that exits without `UBLK_F_USER_RECOVERY` set leaves no path back**: the flag is chosen at `UBLK_CMD_ADD_DEV` time, so a device created without it cannot be re-attached to a replacement daemon after the fact.
- **Committing a result without re-arming the tag silently starves it.** Using a plain commit instead of `UBLK_U_IO_COMMIT_AND_FETCH_REQ` breaks the one-outstanding-command-per-tag invariant, and requests later assigned to that tag have no parked command to complete against.
- **`queue_depth` and `nr_hw_queues` are fixed at device creation.** A device added with one hardware queue will not use additional daemon threads however many cores the load is spread over; the device must be deleted and re-added.
- **Benchmarks against the loop target measure the backing filesystem, not ublk.** Page-cache hits and the underlying filesystem's write path dominate; the null target is what isolates the FETCH/COMMIT transport.
- **Expecting zero-copy on 6.0 misreads the design.** The original driver copies data between the request's pages and the daemon's buffer, so per-I/O cost scales with block size independently of the transport.
- **Only one daemon owns `/dev/ublkc0` per device.** The character node is the private kernel-to-daemon channel, not a general interface a second process can open to inspect or share the device.
