---
title: "Erasure coding: how object stores get 11 nines without 3x the disks"
date: 2026-08-13
track: distributed-systems
summary: "Reed–Solomon striping cuts storage overhead from 3.0x to 1.4x while tolerating more failures — the trade is repair traffic, which local reconstruction codes (Azure LRC) attack. The overhead math, the durability math behind \"11 nines\", and where EC backfires: small objects and hot data."
reading_time: 6
tags: [erasure-coding, object-storage, reed-solomon, durability, ceph]
sources:
  - title: "Erasure Coding in Windows Azure Storage — Huang et al. (USENIX ATC 2012, Best Paper)"
    url: "https://www.usenix.org/conference/atc12/technical-sessions/presentation/huang"
  - title: "Backblaze Vaults: Zettabyte-Scale Cloud Storage Architecture"
    url: "https://www.backblaze.com/blog/vault-cloud-storage-architecture/"
  - title: "Building and operating a pretty big storage system called S3 — Andy Warfield (All Things Distributed, 2023)"
    url: "https://www.allthingsdistributed.com/2023/07/building-and-operating-a-pretty-big-storage-system.html"
  - title: "Erasure Code Settings (MinIO documentation)"
    url: "https://min.io/docs/minio/linux/reference/minio-server/settings/storage-class.html"
  - title: "Erasure code (Ceph Reef documentation)"
    url: "https://docs.ceph.com/en/reef/rados/operations/erasure-code/"
---

"Design S3" interviews usually stall at the same point: the candidate says "replicate every object three times" and the interviewer asks what that costs at an exabyte. Three copies means buying 3 PB of disk per PB of data. Real object stores don't do that for bulk data — they erasure-code it, which is how you get more durability than 3x replication for half the overhead. Here's the math and the catches.

## Replication vs Reed–Solomon

Reed–Solomon coding RS(k, m) splits an object (or a fixed-size stripe) into **k data shards**, computes **m parity shards**, and spreads all k+m across different disks, hosts, or availability zones. *Any* k of the k+m shards reconstruct the original — you can lose any m. Storage overhead is (k+m)/k, and fault tolerance is decoupled from copy count:

| Scheme | Raw bytes per byte stored | Survives | Repairing 1 lost shard reads |
|---|---|---|---|
| 3x replication | 3.0x | any 2 failures | 1 shard-size copy from a replica |
| RS(6,3) | 1.5x | any 3 failures | 6 shards (6x the lost data) |
| RS(10,4) | 1.4x | any 4 failures | 10 shards (10x the lost data) |

RS(10,4) beats 3x replication on *both* axes that matter for durability and cost — it survives four failures instead of two at less than half the footprint. Backblaze runs 17+3 across 20 pods in a "vault" (1.18x overhead); S3 erasure-codes across hosts and AZs, as Andy Warfield's write-up confirms. So why does anyone still replicate? The third column.

## Repair traffic, and the Azure LRC trick

When a disk dies under replication, you copy each lost object once. Under RS(10,4), reconstructing each lost shard means reading **ten** surviving shards and running the decode — a dead 16 TB disk triggers ~160 TB of cluster reads. Repair traffic competes with foreground I/O, and slow repair directly erodes durability (more on that below).

The Azure **Local Reconstruction Codes** paper (USENIX ATC 2012) attacks exactly this. LRC(12,2,2) keeps 12 data shards, adds 2 *global* parities, and splits the data into two groups of 6, each with its own *local* parity. The dominant failure — one lost shard — is now repaired from the 6 shards in its group, not 12+. Cost: 16/12 = 1.33x overhead, and it tolerates any 3 failures plus the large majority of 4-failure patterns. The paper's punchline: same average repair cost as RS(6,3) but 1.33x instead of 1.5x overhead, which at Azure's scale paid for the research many times over. Ceph and HDFS both grew LRC-style plugins for the same reason.

## The shape of an S3-style store

Two planes, deliberately separated:

- **Metadata path:** key → (stripe layout, shard locations, version). A replicated, strongly consistent index — quorum-replicated and typically [LSM-backed](/articles/distributed-systems/2026-08-11-lsm-trees-vs-b-trees) — handles PUT/GET lookups, multipart bookkeeping, and listing. This is where consistency lives.
- **Data path:** shards land on storage nodes chosen so no two shards of a stripe share a failure domain (disk, host, rack, AZ) — placement is the [consistent-hashing / placement-map problem](/articles/distributed-systems/2026-07-25-consistent-hashing-ring) again. A background repair service watches for missing shards and reconstructs onto fresh disks.

A common production pattern (Azure does this explicitly): writes first land 3x-replicated in an append-only journal for low latency, then sealed extents are erasure-coded lazily and the replicas dropped. Hot recent data pays replication's overhead briefly; cold bulk pays 1.3–1.5x forever.

## Where "11 nines" comes from

Durability claims are a race between failure and repair: you lose data only if **m+1 shards of one stripe die before repair completes**. Ballpark it with a binomial: disk AFR of 1% and a 1-day repair window gives per-window failure probability p ≈ 0.01/365 ≈ 2.7×10⁻⁵. For RS(10,4), losing 5 of 14 shards in one window:

```text
P(loss/window) ≈ C(14,5) · p⁵ = 2002 · (2.7e-5)⁵ ≈ 3e-20
P(loss/year)   ≈ 365 · 3e-20  ≈ 1e-17   →  comfortably past 11 nines
```

Backblaze publishes this exact calculation (open-sourced in their `erasure-coding-durability` repo) for 17+3. Two honest caveats worth volunteering in an interview: the model assumes **independent** failures — a bad drive batch, a rack fire, or a software bug that corrupts whole stripes is correlated and dominates real risk, which is why shards cross failure domains — and it assumes repair actually finishes in the window, so repair bandwidth is a durability parameter, not a nicety.

## When EC hurts

- **Small objects.** A 4 KB object striped RS(10,4) produces fourteen tiny shards; per-shard fixed costs and metadata swamp the savings, and a GET costs k disk reads instead of one. Stores either pack small objects into larger stripes or just replicate them.
- **Hot data.** Every degraded read (one shard slow or lost) becomes a k-way fan-out plus decode — tail latency multiplies. Replicas can serve a hot object from any copy.
- **Overwrites.** Updating one byte means re-encoding the stripe; EC wants immutable, sealed data — which objects conveniently are.

Configuring it is anticlimactic:

```bash
# MinIO: 4 parity shards per erasure set (RS(4,4) on an 8-drive set)
export MINIO_STORAGE_CLASS_STANDARD="EC:4"

# Ceph: RS(4,2) pool, shards never share a host
ceph osd erasure-code-profile set ec42 k=4 m=2 crush-failure-domain=host
ceph osd pool create ecpool erasure ec42
```

**Try next:** clone Backblaze's `erasure-coding-durability` repo and re-run the durability model with a 4% AFR and a 7-day repair window — watch how many nines slow repair burns compared to cheap disks.
