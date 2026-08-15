---
title: "bcachefs in 2026: Out of the Kernel Tree, Not Out of the Game"
date: 2026-08-15
track: linux-tools
summary: "bcachefs was merged in Linux 6.7 (January 2024), marked \"externally maintained\" in 6.17, and deleted from the tree entirely in 6.18 after the Overstreet–Torvalds split — yet development continued out of tree. It now ships as a DKMS module versioned with bcachefs-tools: 1.37 (March 2026) stabilized erasure coding and added Linux 7.0 support, and 1.38.6 (June 2026) dropped the experimental label altogether. What the filesystem offers, and how to run a tiered multi-device array today."
reading_time: 7
tags: [bcachefs, filesystems, dkms, storage, copy-on-write]
sources:
  - title: "Bcachefs removed from the mainline kernel — LWN.net"
    url: "https://lwn.net/Articles/1040120/"
  - title: "bcachefs: Getting Started — bcachefs.org"
    url: "https://bcachefs.org/GettingStarted/"
  - title: "bcachefs: Principles of Operation (April 2026)"
    url: "https://bcachefs.org/bcachefs-principles-of-operation.pdf"
  - title: "Bcachefs goes DKMS after Torvalds' kernel banishment — The Register"
    url: "https://www.theregister.com/2025/09/25/bcachefs_dkms_modules/"
  - title: "Bcachefs 1.37 released with Linux 7.0 support, stable erasure coding — Phoronix"
    url: "https://www.phoronix.com/news/Bcachefs-1.37-Released"
---

**Gist.** bcachefs is a copy-on-write (CoW) filesystem that folds device tiering, replication, checksumming and encryption into a single B+tree-based on-disk format, removing the need for a stack of block-layer components beneath it. It was merged into Linux 6.7 in January 2024, frozen as "externally maintained" in 6.17, and removed from the tree in 6.18; it now ships as a Dynamic Kernel Module Support (DKMS) module versioned in lockstep with `bcachefs-tools`. The cost of that arrangement is an out-of-tree build dependency: every kernel upgrade must rebuild the module before the filesystem can be mounted, which converts a routine update into a potential boot failure.

## The timeline

- **January 2024** — merged in Linux 6.7, flagged `EXPERIMENTAL`.
- **November 2024** — Kent Overstreet suspended for the 6.13 cycle over a Code of Conduct violation (an abusive mailing-list exchange with a memory-management maintainer).
- **June 2025** — repeated disputes about shipping recovery features (notably `journal_rewind`) as late `-rc` "fixes" ended with Torvalds announcing a parting of ways in the 6.17 merge window.
- **September 2025** — Linux 6.17 marks bcachefs **externally maintained**: the in-tree code is frozen and patches are no longer accepted. The DKMS plan is announced around the same time.
- **November–December 2025** — the code is removed for 6.18: "It's now a DKMS module, making the in-kernel code stale, so remove it to avoid any version confusion."

The structural conflict is a release-cadence mismatch. The kernel's rule after `-rc1` admits fixes only; bcachefs was landing data-recovery features that its users needed sooner than the next merge window. Out of tree the two release trains are one: **module and tools carry the same version number and are released together**, on the maintainer's cadence rather than the kernel's. The in-tree arrangement could not couple the two that way, since the userspace tools were never part of the kernel release.

Subsequent releases moved quickly: **erasure coding was declared stable in 1.37 (March 2026)**, which also added Linux 7.0 support, and **1.38.6 (June 2026) removed the experimental designation**.

## On-disk structure

bcachefs stores every kind of metadata — inodes, dirents, extents, snapshots, allocation information — in **one B+tree implementation** rather than in separate purpose-built structures. Nodes are unusually large, **256 KiB by default**, and are **log-structured internally**: updates append to a node's journal area and are merged into sorted sets rather than rewriting the node in place. The consequence is that a small metadata update costs an append, not a 256 KiB write; the counterpart cost is that a read of a node must merge across its sorted sets.

The feature set layered on this structure:

- **Copy-on-write** with **checksumming of both data and metadata** (crc32c by default; xxhash and crc64 available) and **compression** (lz4, zstd, gzip), each settable per file, per directory, or per target.
- **Replication.** `--replicas=2` keeps every extent on two devices. Because the unit of replication is the **extent, not the device**, devices of unequal size can be combined without the wasted capacity an mdraid mirror of mismatched members incurs.
- **Erasure coding.** Reed–Solomon striping in place of whole-extent mirroring, behind an experimental flag for years and **declared stable in 1.37**. It is the newest major feature and has the least field exposure of anything listed here.
- **Tiered storage.** Devices carry labels, and three targets decide placement: `foreground_target` where writes first land, `background_target` where the rebalance thread rewrites data afterwards, and `promote_target` where extents read from slow devices are cached. This is the bcache lineage — solid-state writeback caching in front of rotational disks — implemented inside the filesystem rather than in a separate block-layer cache.
- **Encryption** using **ChaCha20/Poly1305**, an authenticated construction covering **data and metadata**. This differs from dm-crypt beneath a filesystem and from [fscrypt's](/articles/linux-tools/2026-07-31-fscrypt-per-directory-encryption) per-directory model.
- **Snapshots and subvolumes**, writable.

## Configuring a tiered array

On Debian and Ubuntu the upstream apt repository provides matched tools and module; Arch and CachyOS package `bcachefs-dkms` directly. The module builds against current kernels including [7.1](/articles/linux-tools/2026-08-13-linux-7-1-whats-new-for-ops). Recent releases migrate code to Rust, so **the kernel headers must have Rust support enabled** for the DKMS build to succeed.

```bash
apt install bcachefs-tools bcachefs-dkms   # module builds via dkms
modprobe bcachefs

# One SSD in front of two HDDs, every extent stored twice on HDD,
# hot data cached on and first written to flash:
bcachefs format \
  --label=ssd.ssd1 /dev/nvme0n1 \
  --label=hdd.hdd1 /dev/sda \
  --label=hdd.hdd2 /dev/sdb \
  --replicas=2 \
  --foreground_target=ssd \
  --promote_target=ssd \
  --background_target=hdd

mount -t bcachefs /dev/nvme0n1:/dev/sda:/dev/sdb /mnt
bcachefs fs usage -h /mnt        # per-device, per-tier accounting

bcachefs subvolume create /mnt/data
bcachefs subvolume snapshot /mnt/data /mnt/data.2026-08-15
```

The label syntax is `group.device`: `ssd.ssd1` places the NVMe device in a group named `ssd`, which the three target options then reference by group name. Writes land on the NVMe device, the rebalance thread rewrites them to the HDD pair as two copies, and reads of cold extents promote them back to flash. The mount specifies every member device explicitly, separated by colons.

## Repair and recovery

Repair is where the implementation invests most. `bcachefs fsck` reconstructs from redundant metadata, **fsck can be run online** against a mounted filesystem rather than only offline, and `journal_rewind` — the feature whose late submission precipitated the 6.16 dispute — **rolls the whole filesystem back to a point before a corrupting event**.

The countervailing facts are concrete. The DKMS dependency means a distribution kernel upgrade whose module build fails leaves the root filesystem unmountable, so a fallback kernel and a rescue image carrying the module are prerequisites rather than precautions. Erasure coding has been declared stable for months, not years. No published benchmark set separates bcachefs from XFS across a representative range of profiles, so the throughput gap is not quantified here. For a network-attached storage box or a backup target, where tiering and checksums are the deciding properties, the trade is defensible. For a database volume the accumulated operational history is not yet there.

| | bcachefs (1.38.x) | btrfs | ZFS |
|---|---|---|---|
| In mainline kernel | no (DKMS) | yes | no (DKMS/OpenZFS) |
| Checksummed CoW | yes | yes | yes |
| Native tiering/caching | yes | no | partial (L2ARC/special) |
| Erasure coding / parity RAID | stable 2026 | RAID5/6 still unsafe | raidz, mature |
| Native encryption | ChaCha20/Poly1305, all metadata | no | AES-GCM, partial metadata |

## Pitfalls

- **A kernel upgrade whose DKMS build fails yields an unbootable system**, because the root filesystem cannot be mounted without the module. The symptom is a rescue shell with no root device; the cause is that the module is not part of the newly installed kernel package.
- **Mismatched tool and module versions are unsupported.** bcachefs-tools and the DKMS module are released as one version; upgrading one alone (for example, tools from a distribution archive against a module built earlier) is outside the tested combination.
- **Kernel headers built without Rust support fail the module build.** The symptom is a DKMS compile error rather than a mount failure, because recent releases contain Rust code.
- **`--replicas=2` does not by itself pin copies to the slow tier.** Placement is governed by `foreground_target`, `background_target` and `promote_target`; without `background_target`, data written to flash is not rewritten to the rotational devices.
- **Erasure coding has the least production exposure of any feature listed here**, having been declared stable only in 1.37 (March 2026).
- **Omitting a member device from the colon-separated mount argument prevents a normal mount**; a degraded mount requires `-o degraded` and is a distinct, reduced-redundancy state.

**Try next:** build the three-device layout above from loopback files (`truncate -s 10G a.img b.img c.img; losetup ...`), write 5 GB, then run `bcachefs fs usage -h` to observe the rebalance thread drain data from the `ssd` device to the `hdd` pair — then detach one loop device beneath the mounted filesystem and compare the behaviour of `-o degraded` and `bcachefs fsck`.
