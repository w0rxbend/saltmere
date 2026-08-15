---
title: "Erasure coding: eleven nines of durability without 3x the disks"
date: 2026-08-13
track: distributed-systems
summary: "Reed–Solomon striping cuts storage overhead from 3.0x to 1.4x while tolerating more simultaneous failures; the cost is repair traffic, which local reconstruction codes (Azure LRC) reduce. Covers the overhead arithmetic, the durability model behind \"eleven nines\", and the cases where erasure coding backfires: small objects and hot data."
reading_time: 7
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

**Gist.** Storing three full copies of every object costs 3 PB of disk per PB of data, which does not scale to exabyte fleets. Reed–Solomon erasure coding replaces copies with algebraic redundancy: an object is split into *k* data shards plus *m* parity shards, and any *k* of the *k+m* shards reconstruct the original, giving more failure tolerance at roughly 1.3–1.5x overhead. The cost is repair: replacing one lost shard requires reading *k* surviving shards and decoding, so a disk replacement generates an order of magnitude more cluster traffic than a replica copy.

## Replication versus Reed–Solomon

Reed–Solomon coding RS(k, m) splits an object — or a fixed-size stripe of it — into **k data shards**, computes **m parity shards**, and places all k+m shards in distinct failure domains: separate disks, hosts, racks, or availability zones. The reconstruction property is that **any k of the k+m shards suffice**; consequently any m simultaneous losses are survivable. Storage overhead is (k+m)/k, which decouples fault tolerance from copy count.

| Scheme | Raw bytes per byte stored | Survives | Repairing 1 lost shard reads |
|---|---|---|---|
| 3x replication | 3.0x | any 2 failures | 1 shard-size copy from a replica |
| RS(6,3) | 1.5x | any 3 failures | 6 shards (6x the lost data) |
| RS(10,4) | 1.4x | any 4 failures | 10 shards (10x the lost data) |

RS(10,4) dominates 3x replication on both durability and cost: four tolerated failures instead of two, at less than half the footprint. Backblaze runs 17+3 across 20 pods in a "vault", an overhead of 1.18x. Warfield's account of S3 describes erasure-coded shards spread across large numbers of drives and hosts. The remaining advantage of replication is entirely in the third column.

## Repair traffic and local reconstruction codes

Under replication, a dead disk is repaired by copying each lost object once: repair traffic equals lost data. Under RS(10,4), each lost shard is reconstructed by reading **ten surviving shards** and decoding, so a failed 16 TB disk induces on the order of 160 TB of cluster reads. That traffic competes with foreground I/O, and because durability depends on repair completing before further failures accumulate, **slow repair is a durability defect, not only a performance one**.

The Azure **Local Reconstruction Codes (LRC)** work (Huang et al., USENIX ATC 2012) targets this cost. LRC(12,2,2) keeps 12 data shards, adds 2 *global* parity shards, and partitions the data into two groups of six, each group carrying its own *local* parity. **The single-shard failure — the dominant case — is then repaired from the six shards of its own group**, rather than from twelve. The resulting overhead is 16/12 ≈ 1.33x, and the code tolerates any 3 failures plus the large majority of 4-failure patterns. Stated against Reed–Solomon: the same single-shard reconstruction cost as RS(6,3) at 1.33x rather than 1.5x overhead. Ceph ships an LRC erasure-code plugin.

## The shape of an S3-style store

Two planes are kept separate.

- **Metadata path.** Key → (stripe layout, shard locations, version). This index is quorum-replicated, strongly consistent, and typically [LSM-backed](/articles/distributed-systems/2026-08-10-lsm-trees-vs-b-trees); it serves PUT/GET lookups, multipart bookkeeping and listing. **Consistency lives here, not in the data path.**
- **Data path.** Shards are placed such that **no two shards of one stripe share a failure domain**, which is the [consistent-hashing / placement-map problem](/articles/distributed-systems/2026-07-25-consistent-hashing-ring) again. A background repair service detects missing shards and reconstructs them onto fresh disks.

Azure applies a two-stage write path explicitly: writes first land 3x-replicated in an append-only journal, obtaining low write latency; sealed extents are then erasure-coded asynchronously and the replicas released. Recent data therefore pays replication overhead briefly, and cold bulk data pays 1.3–1.5x indefinitely.

## Deriving "eleven nines"

Durability is a race between failure arrival and repair completion: an object is lost only when **m+1 shards of the same stripe fail inside one repair window**. A binomial approximation with an annualised failure rate (AFR) of 1% and a one-day repair window gives a per-window per-disk failure probability p ≈ 0.01/365 ≈ 2.7×10⁻⁵. For RS(10,4), five of the fourteen shards must be lost:

```text
P(loss/window) ≈ C(14,5) · p⁵ = 2002 · (2.7e-5)⁵ ≈ 3e-20
P(loss/year)   ≈ 365 · 3e-20  ≈ 1e-17   →  past eleven nines
```

Backblaze applies the same style of calculation to its 17+3 vaults. Two limits of the model are worth stating explicitly. First, it assumes **independent failures**; a bad drive batch, a rack fire, or a software fault that corrupts whole stripes is correlated, and correlated risk dominates the observed loss rate — which is the reason shards are spread across failure domains in the first place. Second, it assumes repair completes within the assumed window, so **repair bandwidth is an input parameter of the durability figure**.

### Implementation sketch (Scala)

The arithmetic above is short enough to express directly, which makes the sensitivity to the repair window visible.

```scala
final case class Code(k: Int, m: Int):
  def shards: Int = k + m
  def overhead: Double = shards.toDouble / k

/** Probability that a single shard's disk fails within one repair window. */
def perWindow(afr: Double, repairDays: Double): Double = afr * repairDays / 365.0

def choose(n: Int, r: Int): Double =
  (1 to r).foldLeft(1.0)((acc, i) => acc * (n - r + i) / i)

/** Dominant term: exactly m+1 of the k+m shards lost inside one window. */
def lossPerWindow(c: Code, p: Double): Double =
  val lost = c.m + 1
  choose(c.shards, lost) * math.pow(p, lost) * math.pow(1 - p, c.shards - lost)

def lossPerYear(c: Code, afr: Double, repairDays: Double): Double =
  val windows = 365.0 / repairDays
  windows * lossPerWindow(c, perWindow(afr, repairDays))

/** Repair reads per lost shard, in multiples of the shard size. */
def repairAmplification(c: Code): Int = c.k        // Reed–Solomon
def repairAmplificationLrc(groupSize: Int): Int = groupSize  // single-shard case

val rs104 = Code(10, 4)
lossPerYear(rs104, afr = 0.01, repairDays = 1.0)   // ~1e-17
lossPerYear(rs104, afr = 0.04, repairDays = 7.0)   // orders of magnitude worse
```

The two calls at the end isolate the point: **degrading the repair window costs more nines than degrading disk quality**, because the window enters the expression raised to the power m+1.

## Where erasure coding hurts

- **Small objects.** A 4 KB object under RS(10,4) becomes fourteen very small shards; per-shard fixed costs and metadata exceed the capacity saved, and each GET performs k disk reads rather than one. Stores either pack small objects into larger stripes or replicate them.
- **Hot data.** Any degraded read — one shard missing or slow — becomes a k-way fan-out plus a decode, multiplying tail latency. A replicated object can instead be served from whichever copy responds first.
- **Overwrites.** Modifying one byte requires re-encoding the stripe. Erasure coding suits immutable, sealed data, which is what object storage holds.

Configuration is comparatively small:

```bash
# MinIO: 4 parity shards per erasure set (RS(4,4) on an 8-drive set)
export MINIO_STORAGE_CLASS_STANDARD="EC:4"

# Ceph: RS(4,2) pool, shards never share a host
ceph osd erasure-code-profile set ec42 k=4 m=2 crush-failure-domain=host
ceph osd pool create ecpool erasure ec42
```

## Pitfalls

- Placing shards without a failure-domain constraint produces stripes whose shards share a rack or host; a single rack loss then removes more than m shards at once and the binomial durability estimate no longer applies.
- Sizing repair bandwidth from average load ignores that repair traffic is bursty and proportional to k; during a disk replacement the cluster reads roughly k times the lost capacity, and foreground latency degrades until repair drains.
- Quoting a nines figure computed under independent failures conceals the dominant real risk: correlated events such as a bad drive batch or a bug that corrupts every shard of a stripe identically.
- Erasure-coding small objects directly inflates metadata and turns each GET into k reads; the capacity saved on a 4 KB object is smaller than the per-shard overhead it introduces.
- Treating an erasure-coded pool as overwritable forces a full stripe re-encode per modification; the code is defined over a sealed stripe, not over individual bytes.
- Reducing m to save capacity also reduces the exponent m+1 in the loss expression, so a one-shard reduction in parity moves durability by orders of magnitude, not by a fraction.
