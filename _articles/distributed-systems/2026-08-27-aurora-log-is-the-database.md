---
title: "Amazon Aurora: The Log Is the Database, and 4-of-6 Quorums Across Three AZs"
date: 2026-08-27
track: distributed-systems
summary: "Aurora ships only redo log records from the database engine to a storage fleet that materializes pages itself, so no page ever crosses the network. Data is replicated six ways across three availability zones with a 4/6 write quorum and 3/6 read quorum, sized so that an entire zone plus one more node can fail without losing data. Covers the quorum arithmetic, the LSN bookkeeping (SCL, PGCL, VCL) that lets the primary read from a single storage node instead of a quorum, and the 10 GB segment as the unit of repair."
reading_time: 8
tags:
- aurora
- quorum
- replication
- redo-log
- write-amplification
- storage-disaggregation
sources:
- title: "Verbitski et al. — Amazon Aurora: Design Considerations for High Throughput Cloud-Native Relational Databases (SIGMOD 2017)"
  url: https://dl.acm.org/doi/10.1145/3035918.3056101
- title: "Verbitski et al. — Amazon Aurora: On Avoiding Distributed Consensus for I/Os, Commits, and Membership Changes (SIGMOD 2018)"
  url: https://pages.cs.wisc.edu/~yxy/cs839-s20/papers/aurora-sigmod-18.pdf
- title: "The Morning Paper — Amazon Aurora: design considerations for high throughput cloud-native relational databases"
  url: https://blog.acolyer.org/2019/03/25/amazon-aurora-design-considerations-for-high-throughput-cloud-native-relational-databases/
- title: "The Morning Paper — Amazon Aurora: on avoiding distributed consensus for I/Os, commits, and membership changes"
  url: https://blog.acolyer.org/2019/03/27/amazon-aurora-on-avoiding-distributed-consensus-for-i-os-commits-and-membership-changes/
---

**Gist.** A conventional replicated MySQL deployment writes each change many times: the redo log, the binary log, the data page, the double-write buffer — and then mirrors all of it to a standby. Aurora (Verbitski et al., SIGMOD 2017) removes every one of those page writes from the network: **the database engine ships only redo log records** to a purpose-built storage fleet, and the storage nodes replay the log to materialize pages themselves. Durability comes from replicating each record to **six storage segments across three availability zones (AZs), acknowledging a write at 4 of 6**. The cost is a storage tier that must understand the database's log format, and a primary that must track, record by record, which of its six replicas has what.

## Why the log suffices

A relational engine's redo log is already a complete, ordered description of every change: each record says "apply this delta to this page, at this log sequence number (LSN)". A data page is therefore a cache — the result of applying a prefix of the log to an earlier page image. Aurora takes that observation literally. The engine never writes a page anywhere: *"no pages are ever written from the database tier, not for background writes, not for checkpointing, and not for cache eviction"* (SIGMOD 2017). Each storage node receives the stream of redo records for the pages it hosts, appends them durably, acknowledges, and **applies them to pages in the background**, on its own schedule.

Two consequences follow directly.

- **Write amplification collapses.** The only bytes crossing the network are compact redo records, not 16 KB pages plus their double-write copies plus a binlog. In the paper's 30-minute SysBench write-only benchmark on a 100 GB dataset, Aurora processed **35× the transactions of mirrored MySQL** on equivalent hardware, precisely because the mirrored configuration amplified each logical write into many physical ones.
- **Crash recovery disappears as a startup phase.** In a traditional engine, recovery means replaying the log from the last checkpoint before accepting queries. In Aurora, redo application *is* the storage tier's steady-state job — continuous, asynchronous, distributed across the fleet — so a restarted primary does not replay anything; it establishes the durable point (below) and serves.

## The quorum arithmetic: why 4 of 6

A quorum system over V replicas needs two inequalities: **V_r + V_w > V** (a read quorum intersects every write quorum, so a reader sees the latest acknowledged write) and **V_w > V/2** (two write quorums intersect, so conflicting writes cannot both be acknowledged). The common choice V = 3, V_w = V_r = 2 satisfies both — and is, the paper argues, inadequate in a large fleet, because failures are not independent. An AZ is a correlated failure domain: a fire, a flood, or a network partition takes out every replica inside it at once. With three replicas in three AZs, an AZ failure plus one unrelated node failure (a bad disk, a rebooting host — routine at fleet scale) destroys two of three copies and breaks quorum.

Aurora sizes the quorum for exactly that scenario, which the paper names **"AZ+1"**: tolerate the loss of an entire AZ *plus* one additional node without losing data. Six copies, **two per AZ**, with **V_w = 4 and V_r = 3**:

- 3 + 4 > 6 and 2 × 4 > 6, so both intersection properties hold.
- **Losing one whole AZ** removes two copies; four remain, so the volume can still assemble a 4/6 write quorum — writes continue.
- **Losing an AZ plus one more node** removes three; the surviving three still form a 3/6 read quorum, so no acknowledged write is lost. Write availability is gone until repair, but the read quorum is exactly what repair needs: it can reconstruct the missing segments and re-establish a writable quorum.

The quorum is deliberately **asymmetric**: the write quorum is sized for durability under correlated failure, and the read quorum is the smallest set guaranteed to intersect it. What makes six-way replication affordable is that the six copies hold log records and pages, not six synchronously mirrored full database stacks — and, per the SIGMOD 2018 paper, the quorum is also **heterogeneous**: three *full segments* store both redo records and materialized pages, three *tail segments* store redo records only, and a write quorum is either any 4 of 6 segments or all 3 full segments.

## Segments: bounding the blast radius

Quorum math bounds *how many* failures are survivable; the unit of replication bounds *how long* a failure window stays open. Aurora partitions each volume into **10 GB segments**, each independently replicated six ways as a **protection group**. The size is chosen against the network: a 10 GB segment transfers over a 10 Gbps link in roughly **10 seconds**, so a lost replica is re-replicated in seconds rather than the hours a full-volume copy would take. Losing quorum then requires two segment failures *plus* an AZ failure, all inside the same ten-second repair window — the design pushes the double-fault probability down by shrinking the exposure time rather than by adding still more copies. Segment membership changes use epochs and overlapping quorum sets (writes must satisfy both the old and the new group during a transition), so repair never passes through a state with an ambiguous quorum.

## Avoiding quorum reads entirely

The textbook cost of a read quorum is heavy: fetch the page from V_r nodes, compare versions, keep the newest. Aurora never pays it on the read path. The primary is the **only writer**, and it observes every acknowledgement, so it can maintain per-segment bookkeeping:

- **SCL (segment complete LSN)** — the highest LSN below which a given storage node has received every record for its segment (nodes also gossip within a protection group to fill holes).
- **PGCL (protection group complete LSN)** — the point a protection group reaches once 4 of its 6 segments have advanced their SCL past it.
- **VCL (volume complete LSN)** — the highest LSN at which the whole volume is complete; the volume durable point below which recovery truncates.

A commit is acknowledged once the VCL passes the commit record's LSN — and every log write, storage-side processing step, and acknowledgement in that pipeline is asynchronous; there is no synchronous consensus round per I/O. For reads, the primary establishes a read point at the current durable LSN and, **because it knows each segment's SCL, sends the request to a single storage node it already knows is complete to that point** — typically the lowest-latency one. The quorum intersection property is enforced by bookkeeping at the writer instead of by voting at read time: a 1-node read replaces a 3-node read, with no version comparison. Quorum reads survive only in crash recovery, when a newly started primary must reconstruct the VCL by asking the segments what they hold.

### Implementation sketch (Scala)

The load-bearing state machine is the LSN bookkeeping. The sketch tracks acknowledgements for one protection group and shows both decisions: when a write is durable, and which single node may serve a read.

```scala
final case class Segment(id: Int, az: Int, scl: Long) // scl: complete up to here

final case class ProtectionGroup(segments: Vector[Segment]) {
  val writeQuorum = 4 // of 6, two per AZ across three AZs

  def ack(segId: Int, lsn: Long): ProtectionGroup =
    copy(segments = segments.map { s =>
      if (s.id == segId && lsn > s.scl) s.copy(scl = lsn) else s
    })

  /** PGCL: highest LSN that >= 4 of 6 segments have fully received.
    * The 4th-highest SCL is exactly that point. */
  def pgcl: Long =
    segments.map(_.scl).sorted(Ordering[Long].reverse)(writeQuorum - 1)

  def durable(commitLsn: Long): Boolean = pgcl >= commitLsn

  /** No quorum read: any single segment complete to the read point serves. */
  def readNode(readPoint: Long): Option[Segment] =
    segments.filter(_.scl >= readPoint).minByOption(_.id) // pick by latency in practice

  /** AZ+1: drop one whole AZ and one more node; a 3/6 read quorum must survive. */
  def survivorsAfterAzPlusOne(lostAz: Int, lostNode: Int): Vector[Segment] =
    segments.filterNot(s => s.az == lostAz || s.id == lostNode)
}
```

`pgcl` is monotone because each `scl` is, so `durable` never regresses — the property that lets commits be acknowledged from bookkeeping alone, without a per-commit vote.

## Pitfalls

- **Treating 4/6–3/6 as generic quorum tuning misses the failure model.** The sizes fall out of "survive AZ+1", a correlated-failure requirement; the same arithmetic over six uncorrelated nodes would justify nothing.
- **Assuming reads are quorum reads.** Aurora's steady-state reads touch one storage node; the intersection guarantee lives in the primary's SCL tracking. Modelling read latency or read fan-out as V_r-way is wrong for this system.
- **After an AZ+1 failure the volume is readable but not writable.** Three survivors form a read quorum only; writes resume when repair rebuilds enough segments for a 4/6 write quorum.
- **The single-writer assumption is load-bearing.** The bookkeeping that replaces quorum reads works because exactly one engine issues and observes all writes; a second uncoordinated writer would invalidate every SCL the first one holds.
- **Segment size trades repair speed against bookkeeping volume.** 10 GB pins the repair window near 10 seconds on a 10 Gbps link; larger segments widen the double-fault window, smaller ones multiply protection groups (a 64 TB volume already has 38,400 segments).
