---
title: "ublk: serving a block device from userspace over io_uring"
date: 2026-08-03
track: linux-tools
summary: "A block device that appears in lsblk but whose reads and writes are handled by an ordinary userspace process. ublk_drv, merged in Linux 6.0, uses io_uring command passthrough to hand each I/O request to a daemon and collect the result — giving you the ergonomics of FUSE-for-block without NBD's socket overhead or TCMU's SCSI baggage."
reading_time: 6
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

A FUSE filesystem lets an unprivileged process pretend to be a filesystem. ublk is the block-layer version of that idea: a `/dev/ublkb0` shows up in `lsblk`, `blk-mq` schedules requests against it, `mkfs` and `mount` treat it as a normal disk — but every read and write is actually serviced by an ordinary userspace daemon. The kernel piece, `ublk_drv`, landed in **Linux 6.0** (October 2022). What makes it interesting is the transport: instead of a socket (NBD) or a shared SCSI ring (TCMU), it moves each I/O across the kernel/userspace boundary as an **io_uring passthrough command**.

## Two device nodes, two planes

ublk splits cleanly into a control plane and a data plane, and that split is visible as device nodes.

There is one global control node, `/dev/ublk-control`. The daemon issues management commands to it via io_uring — `UBLK_CMD_ADD_DEV` (with queue count, block size, capacity), `UBLK_CMD_START_DEV`, and so on. These are not ioctls; they ride io_uring's command opcode (`IORING_OP_URING_CMD`), which is the whole point.

Each device you add produces two more nodes:

- `/dev/ublkc0` — a **character** device. This is the private channel between the kernel driver and the one daemon that owns the device. I/O descriptors are `mmap`'d through it.
- `/dev/ublkb0` — the **block** device. This is what the rest of the system sees; it is driven by a request-based `blk-mq` driver, so it gets multiqueue, schedulers, and everything else the block layer offers.

## The FETCH / COMMIT loop

The data plane is a single tight loop, and understanding it is understanding ublk. Per the kernel docs, each queue is served by a daemon thread that owns an io_uring instance. I/O requests carry a queue-wide unique **tag**, and the daemon keeps one outstanding io_uring command per tag.

1. The daemon submits `UBLK_U_IO_FETCH_REQ` for a tag — "sent from the server I/O pthread for fetching future incoming I/O requests destined to `/dev/ublkb*`." This command parks in the kernel, waiting.
2. Something touches `/dev/ublkb0`. `blk-mq` builds a request, the kernel fills in the shared `ublksrv_io_desc` (op, sector, length) for that tag, and **completes the parked FETCH command**. The daemon's io_uring now hands it a CQE — that is the I/O notification.
3. The daemon does the actual work: for a loop target it `pread`/`pwrite`s the backing file; for a network target it talks to a server; for null it does nothing.
4. The daemon reports completion *and* re-arms in one shot with `UBLK_U_IO_COMMIT_AND_FETCH_REQ` — commit the result of the finished tag, and simultaneously fetch the next request for that tag.

So steady state is a stream of `COMMIT_AND_FETCH` commands: no ioctl, no socket `send`/`recv`, no syscall per I/O beyond io_uring's normal batched submission. The data itself is copied between the request's pages and the daemon's buffer (true zero-copy was a later, separately landed feature; the original 6.0 design copies).

## Driving it with the `ublk` CLI

The reference userspace lives in the [ublksrv](https://github.com/ublk-org/ublksrv) project (originally Ming Lei's `ubdsrv`). It builds a daemon plus an `ublk` control binary. You need `liburing >= 2.2` and a 6.0+ kernel with `CONFIG_BLK_DEV_UBLK`.

```console
# load the driver
$ sudo modprobe ublk_drv

# a zero-overhead null device — great for benchmarking the plumbing
$ sudo ublk add -t null
dev id 0: nr_hw_queues 1 queue_depth 128 block size 512 dev_capacity ...

# a real disk backed by an image file
$ truncate -s 1G ublk-loop.img
$ sudo ublk add -t loop -f ublk-loop.img
dev id 1: nr_hw_queues 1 queue_depth 128 ...

$ lsblk
NAME    MAJ:MIN RM  SIZE RO TYPE MOUNTPOINTS
ublkb0  259:0    0    0B  0 disk
ublkb1  259:1    0    1G  0 disk

# it's an ordinary block device from here on
$ sudo mkfs.xfs /dev/ublkb1
$ sudo mount /dev/ublkb1 /mnt

$ ublk list -v          # inspect queues, tags, daemon pid
$ ublk del -n 1         # remove one device
$ ublk del -a           # remove all
```

If the daemon crashes, the device does not silently corrupt: ublk supports a recovery mode (`UBLK_F_USER_RECOVERY`) where a new daemon can re-attach and re-fetch outstanding tags. That crash-recovery story is one of the things NBD historically handled poorly.

## Where it sits versus NBD, TCMU, and FUSE

All four move device semantics into userspace; the differences are in the wire and the scaling.

- **NBD** sends requests over a socket. It works everywhere and over the network, but every I/O is a socket round trip, and its multiqueue and reconnect behavior have long been rough edges. In Ming Lei's early numbers, a ublk-based qcow2 target hit roughly **2x the IOPS of `qemu-nbd`**.
- **TCMU** exposes userspace targets through the SCSI target (LIO) stack. That means SCSI command overhead and a single shared command ring guarded by coarse locking — no real multiqueue. ublk gives each queue its own io_uring instance, so it scales with jobs.
- **FUSE** is the closest conceptual cousin, but it is a *filesystem* interface, not a block one, and its transport is the older FUSE protocol rather than io_uring. ublk is essentially "FUSE for block devices, on io_uring."

The strategic bet is that io_uring command passthrough is a good enough kernel/userspace channel that whole classes of drivers — qcow2, encrypted or deduplicated volumes, network-backed disks — can live in userspace without paying for it in throughput. `ublk_drv` is small precisely because it delegates all the interesting logic outward.

**Try next:** `sudo ublk add -t null` then run `fio --name=t --filename=/dev/ublkb0 --rw=randread --bs=4k --iodepth=64 --ioengine=io_uring` and watch a userspace daemon service the whole thing — then `ublk list -v` in another shell to see the per-tag io_uring commands in flight.
