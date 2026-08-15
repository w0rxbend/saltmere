---
title: "GFS: The Google File System Paper"
date: 2026-08-15
track: distributed-systems
summary: "The SOSP '03 paper that made 'assume components fail' a design axiom: one master holding all metadata in RAM (<64 bytes per 64MB chunk) with an operation log and shadow masters, chunkservers with 3x replication, 60-second leases serializing mutation order, and record append that promises at-least-once — duplicates and padding included. Plus the single master's limits as cells grew into the tens of petabytes and how Colossus (curators over Bigtable) replaced it."
reading_time: 7
tags: [gfs, distributed-file-system, paper-review, leases, replication, google]
sources:
  - title: "Ghemawat, Gobioff & Leung — The Google File System (SOSP 2003)"
    url: "https://research.google.com/archive/gfs-sosp2003.pdf"
  - title: "McKusick & Quinlan — GFS: Evolution on Fast-forward (ACM Queue, 2009)"
    url: "https://queue.acm.org/detail.cfm?id=1594206"
  - title: "Google Cloud Blog — A Peek Behind Colossus, Google's File System"
    url: "https://cloud.google.com/blog/products/storage-data-transfer/a-peek-behind-colossus-googles-file-system"
---

**Gist.** A storage cluster built from a thousand commodity machines fails continuously, so the Google File System (GFS) treats failure as the steady state rather than an exception. It centralises all metadata in a single master held in memory and made durable by a replicated operation log, delegates per-chunk mutation ordering to a leased primary replica, and offers an append primitive whose guarantee is atomic *at-least-once* placement. The cost is paid twice: applications must tolerate duplicate and padded records, and the single master eventually bounds both cluster capacity and file count.

The paper (Ghemawat, Gobioff & Leung, SOSP 2003) states the axiom directly — with clusters spanning hundreds or thousands of commodity machines, "component failures are the norm rather than the exception." Three workload observations complete the premise: files are multi-gigabyte, writes are overwhelmingly *appends* rather than overwrites, and reads are large sequential streams. Most of the design follows from those four statements.

## One master, 64MB chunks, metadata in RAM

Files are split into **64MB chunks**, each identified by an immutable 64-bit handle and stored as an ordinary Linux file on a **chunkserver**, replicated **three ways by default**. A **single master** holds the namespace, the file→chunk mapping, and the chunk→replica locations. Data never passes through the master: a client requests chunk locations, caches them, and then exchanges bytes with chunkservers directly.

The chunk size is what makes the metadata arithmetic close. Larger chunks mean fewer location lookups per byte read, longer-lived client-to-chunkserver TCP connections, and **fewer than 64 bytes of metadata per chunk**, so the entire map fits in one machine's RAM.

Durability of that in-memory image rests on a **write-ahead operation log** — the mechanism described in [WAL and crash recovery](/articles/distributed-systems/2026-08-10-write-ahead-log-wal) — replicated to remote machines *before* any mutation is acknowledged, with periodic checkpoints in a compact B-tree-like form so that replay after a restart covers only the tail. Chunk *locations* are not logged. The master reconstructs them by polling chunkservers at startup and via heartbeats, on the grounds stated in the paper that a chunkserver "has the final word over what chunks it does or does not have." **Shadow masters** replay the same log and can serve slightly stale reads while the primary master is unavailable; they are read-only spares, not a consensus-based failover group.

## Leases and the mutation pipeline

Concurrent writers to one chunk require a single mutation order, and the master cannot be consulted per write. GFS grants a **60-second lease** on a chunk to one replica, the **primary**, extendable by piggybacked heartbeat messages. The primary assigns a serial number to every mutation on that chunk; secondaries apply mutations in that order. The invariant is temporal, as in [leases as coordination](/articles/distributed-systems/2026-08-13-leases-time-based-coordination): the master may grant a new lease only after the previous one has *expired*, so two primaries for the same chunk never hold overlapping validity intervals even when the master cannot reach the old primary.

**Control flow and data flow are separated.** The payload is pushed linearly along a chain of chunkservers — each forwards to the *nearest* replica that does not yet hold the data, pipelining bytes as they arrive — so each machine spends its full outbound bandwidth on one downstream peer instead of fanning out from the client. Only once every replica has buffered the bytes does the client send the write request to the primary, which assigns the offset and order, and then to the secondaries.

## Record append: atomic at least once

The distinguishing primitive is **record append**: the client supplies the data and GFS chooses the offset. Many producers can append to one file without external synchronisation. The guarantee is deliberately weak — the record is written **atomically at least once** at *some* offset chosen by the primary.

Two consequences follow mechanically. If an append would straddle a chunk boundary, the primary **pads the current chunk** and the operation retries on a fresh one; the paper bounds this waste by restricting append size to **one quarter of the chunk size**. If any replica fails part-way through, the client retries the whole append, so replicas may contain **duplicate records**, and a region may be written on one replica and not another.

The resulting vocabulary: a region is **consistent** when all clients see the same data regardless of replica, and **defined** when it is consistent *and* the effect of one mutation is visible in its entirety. Serial successful writes are defined; concurrent successful overwrites are consistent but undefined, because fragments from several mutations interleave; a failed mutation leaves the region inconsistent. The paper's response is co-design above the file system: applications self-delimit records with checksums, carry unique record identifiers so readers can drop duplicates, and prefer appending followed by an atomic rename.

| Mutation | Guarantee | Application burden |
|---|---|---|
| Serial write | defined | none |
| Concurrent write | consistent, undefined | avoid; use record append |
| Record append | atomic, at least once | checksums plus deduplication identifiers |
| Failed mutation | inconsistent until re-replicated | retry |

### Implementation sketch (Scala)

The load-bearing state is the master's lease table and the primary's serial counter. The sketch below models the lease invariant and the padding branch; replication, checksums and RPC are omitted.

```scala
opaque type ChunkId = Long

final case class Lease(primary: String, expiresAt: Long)

final class LeaseTable(durationMillis: Long = 60_000):
  private var leases: Map[ChunkId, Lease] = Map.empty

  /** The current primary may extend; a different replica waits for expiry. */
  def grant(chunk: ChunkId, replica: String, now: Long): Option[Lease] =
    leases.get(chunk) match
      case Some(l) if l.expiresAt > now && l.primary != replica => None
      case _ =>
        val l = Lease(replica, now + durationMillis)
        leases = leases.updated(chunk, l)
        Some(l)

enum AppendResult:
  case Written(offset: Long)
  case Padded                 // record would straddle the boundary
  case ReplicaFailed          // client retries; duplicates become possible

final class Primary(chunkSize: Long):
  private var offset: Long = 0L
  private var serial: Long = 0L

  def append(record: Array[Byte], leaseValid: Boolean): AppendResult =
    require(record.length <= chunkSize / 4)      // paper's bound on padding waste
    if !leaseValid then AppendResult.ReplicaFailed
    else if offset + record.length > chunkSize then
      offset = chunkSize                          // pad out; caller retries on a new chunk
      AppendResult.Padded
    else
      serial += 1
      val at = offset
      offset += record.length
      AppendResult.Written(at)
```

## What broke, and what Colossus changed

The 2009 ACM Queue interview with Sean Quinlan, **"GFS: Evolution on Fast-forward"**, records the limits observed in production. As storage grew "from a few hundred terabytes up to petabytes and then up to tens of petabytes," the single master's memory and the throughput of its single namespace lock became the constraint. Because metadata cost scales with chunk count rather than byte count, **file count binds before capacity does**: a million small files consume master memory comparable to far more data held in large ones. Latency-sensitive serving workloads such as Gmail also could not absorb the master recovery pauses that batch pipelines tolerated.

**Colossus** retains chunkservers, now called D file servers, and replaces the single master with **curators** — horizontally scalable metadata services that store file system metadata **in Bigtable**, which itself runs on Colossus, with bootstrap instances terminating the recursion. Background **custodians** perform repair and rebalancing. Replication is supplemented by Reed–Solomon codes for storage cost, the technique covered in [erasure coding in object storage](/articles/distributed-systems/2026-08-13-erasure-coding-object-storage). Google describes single Colossus clusters holding exabytes of data.

As a design skeleton the structure survives: separate metadata from data, hold metadata in memory behind a replicated log, lease out ordering to one replica per unit of data, pipeline payload along a replica chain, and choose consistency semantics that match the workload rather than paying for full POSIX semantics. The known cliff is the single master, and the documented remedy is sharding metadata over a scalable key-value store.

**Further work.** Section 2.7 of the paper covers the consistency model, section 3.1 the lease and mutation order, and section 3.3 atomic record appends. A single-machine simulation — three replica processes, one primary with an expiring lease, and a record-append client that kills a replica mid-append at random — makes the duplicate and padded record counts measurable over ten thousand appends; adding client-side record identifiers and a deduplicating reader restores exactly-once at the application layer.

## Pitfalls

- **Treating record append as exactly-once.** A reader that counts records directly over-counts, because a client retry after a partial replica failure leaves the earlier copy in place on the replicas that succeeded.
- **Reading a chunk without record framing.** Padding inserted at a chunk boundary is arbitrary filler, so a parser that assumes contiguous records will consume it as data; records must be self-delimiting and checksummed.
- **Assuming a shadow master is a failover master.** Shadow masters replay the operation log and serve stale reads only; a write issued against one has no path to durability.
- **Many small files.** Master memory is consumed per chunk, not per byte, so a workload of millions of small files exhausts the metadata budget long before the chunkservers' disks fill.
- **Concurrent overwrites at a fixed offset.** The region is consistent but undefined: every replica shows the same bytes, and those bytes may be an interleaving of fragments from several writers, with no writer's record present in full.
- **Relying on the master's cached locations after a lease change.** A client holding a stale primary identity sends its write request to a replica whose lease has expired, and the mutation is rejected rather than silently ordered.
