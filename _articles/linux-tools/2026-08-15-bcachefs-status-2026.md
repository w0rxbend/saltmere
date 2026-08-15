---
title: "bcachefs in 2026: Out of the Kernel Tree, Not Out of the Game"
date: 2026-08-15
track: linux-tools
summary: "bcachefs was merged in Linux 6.7 (January 2024), marked \"externally maintained\" in 6.17, and deleted from the tree entirely in 6.18 after the Overstreet–Torvalds split — yet development sped up. It now ships as a DKMS module versioned with bcachefs-tools: 1.37 (March 2026) stabilized erasure coding and added Linux 7.0 support, and 1.38.6 (June 2026) dropped the experimental label altogether. What the filesystem actually offers, and how to run a tiered multi-device array today."
reading_time: 6
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

Few filesystems have had a stranger trajectory than **bcachefs**: a decade out of tree, merged into Linux 6.7 in January 2024 as the great experimental hope for a trustworthy copy-on-write filesystem, and deleted from the kernel again 23 months later — while simultaneously getting *better*. If you stopped tracking it during the mailing-list drama, the operator-relevant summary is: it now lives as a **DKMS module** shipped alongside `bcachefs-tools`, the pace of releases has increased, erasure coding went stable in **1.37** (March 15, 2026), and the **1.38.6** "performance release" (June 19, 2026) removed the experimental designation entirely. Whether you should trust it with data is a different question than whether it's interesting — it is now defensible on both counts, with caveats.

## The timeline, precisely

- **January 2024** — merged in Linux 6.7, flagged `EXPERIMENTAL`.
- **November 2024** — Kent Overstreet suspended for the 6.13 cycle over a Code of Conduct violation (an abusive mailing-list exchange with a memory-management maintainer).
- **June 2025** — repeated fights about shipping recovery features (notably `journal_rewind`) as late `-rc` "fixes" ended with Torvalds writing that "we'll be parting ways in the 6.17 merge window."
- **September 2025** — Linux 6.17 marks bcachefs **externally maintained**: code frozen in-tree, patches no longer accepted. Overstreet announces the DKMS plan the same week.
- **November–December 2025** — Torvalds removes the code for 6.18: "It's now a DKMS module, making the in-kernel code stale, so remove it to avoid any version confusion."

Both sides had a point. Overstreet was landing genuine data-recovery fixes for real users at speeds the kernel's rc discipline doesn't allow; Torvalds runs a process where "fixes only after rc1" is non-negotiable for everyone. Out of tree, that tension simply vanishes: the module and the tools release together, versioned as one, on Overstreet's cadence. The cost is on you — a filesystem your kernel can't mount without a third-party module is a real operational dependency, exactly the class of risk ZFS users have managed for fifteen years.

## What you actually get

Technically, bcachefs is the most ambitious general-purpose Linux filesystem design since ZFS. Everything lives in one **B+tree** implementation with unusually large nodes (256KiB default, log-structured internally), which is where much of its metadata performance comes from. On top of that:

- **Copy-on-write** with full data and metadata **checksumming** (crc32c default, xxhash/crc64 optional) and **compression** (lz4, zstd, gzip), settable per file, directory, or target.
- **Replication**: `--replicas=2` mirrors every extent across devices; unlike mdraid it is extent-granular, so mixed-size devices work.
- **Erasure coding**: Reed–Solomon striping instead of mirroring — declared stable in 1.37 after years behind an experimental flag. Newest major feature; treat it with proportional respect.
- **Tiered storage**: devices carry labels; `foreground_target` picks where writes land, `background_target` where data is rewritten to in the background, `promote_target` where hot data is cached on read. This is the bcache heritage — SSD writeback caching in front of spinning disks, native to the filesystem.
- **Encryption** with **ChaCha20/Poly1305** — authenticated encryption covering data *and* metadata, a stronger design than dm-crypt-under-a-filesystem or [fscrypt's](/articles/linux-tools/2026-07-31-fscrypt-per-directory-encryption) per-directory model.
- **Snapshots** and subvolumes, writable and cheap.

## Hands-on: a tiered array

On Debian/Ubuntu, Overstreet's apt repository (and, increasingly, distro archives — Arch and CachyOS package `bcachefs-dkms` directly) provides matched tools and module; the DKMS module builds against current kernels including [7.1](/articles/linux-tools/2026-08-13-linux-7-1-whats-new-for-ops), and note that recent releases are migrating code to Rust, so your kernel headers must have Rust support enabled (all major distro kernels now do):

```bash
apt install bcachefs-tools bcachefs-dkms   # module builds via dkms
modprobe bcachefs

# One SSD in front of two HDDs, everything stored twice on HDD,
# hot data cached and first-written on flash:
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

Writes land on the NVMe, get rewritten to the HDD pair (two copies) in the background, and reads promote hot extents back to flash. That configuration — one command, no lvmcache/mdraid/dm-crypt stack — is the honest pitch for bcachefs.

## Is it safe yet?

The 2026 answer is "cautiously, with backups you'd want anyway." In its favor: the repair story is unusually strong — `bcachefs fsck` is aggressive about reconstructing from redundant metadata, every fsck pass has an online counterpart, and `journal_rewind` (the feature that triggered the 6.16 blowup) can roll the entire filesystem back to a pre-corruption point; 1.37 declared it safe for general use. Overstreet's stated bar is that filesystems should be "bulletproof," and user reports of unrecoverable filesystems have become genuinely rare. Against it: the DKMS dependency means a distro kernel upgrade can leave you unbootable if the module fails to build (keep a fallback kernel and a rescue image that carries the module — SystemRescue does since 13.x); erasure coding is one year past its stability declaration; and performance still trails XFS on some profiles (Overstreet's own June 2026 numbers show ~700K random-write IOPS where XFS hits 1M). For a NAS, a backup target, or any box where tiering and checksums matter more than the last 30% of IOPS, it's a credible choice in 2026. For the database volume, not yet — not because it eats data, but because it hasn't had the decade of boring that earns that job.

| | bcachefs (1.38.x) | btrfs | ZFS |
|---|---|---|---|
| In mainline kernel | no (DKMS) | yes | no (DKMS/OpenZFS) |
| Checksummed CoW | yes | yes | yes |
| Native tiering/caching | yes | no | partial (L2ARC/special) |
| Erasure coding / parity RAID | stable 2026 | RAID5/6 still unsafe | raidz, mature |
| Native encryption | ChaCha20/Poly1305, all metadata | no | AES-GCM, partial metadata |

**Try next:** build the three-device layout above out of loopback files (`truncate -s 10G a.img b.img c.img; losetup ...`), write 5GB, then `bcachefs fs usage -h` to watch the rebalance thread drain data from the "ssd" device to the "hdd" pair — then pull one loop device out from under a mounted filesystem and see what `-o degraded` and `bcachefs fsck` actually do.
