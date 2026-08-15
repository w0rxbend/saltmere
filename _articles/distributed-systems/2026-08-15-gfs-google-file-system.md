---
title: "GFS: The Google File System Paper"
date: 2026-08-15
track: distributed-systems
summary: "The SOSP '03 paper that made 'assume components fail' a design axiom: one master holding all metadata in RAM (<64 bytes per 64MB chunk) with an operation log and shadow masters, chunkservers with 3x replication, 60-second leases serializing mutation order, and record append that promises at-least-once — duplicates and padding included. Plus why the single master capped GFS near the petabyte/50M-file mark and how Colossus (curators over Bigtable) replaced it."
reading_time: 6
tags: [gfs, distributed-file-system, paper-review, leases, replication, google]
sources:
  - title: "Ghemawat, Gobioff & Leung — The Google File System (SOSP 2003)"
    url: "https://research.google.com/archive/gfs-sosp2003.pdf"
  - title: "McKusick & Quinlan — GFS: Evolution on Fast-forward (ACM Queue, 2009)"
    url: "https://queue.acm.org/detail.cfm?id=1594206"
  - title: "Google Cloud Blog — A Peek Behind Colossus, Google's File System"
    url: "https://cloud.google.com/blog/products/storage-data-transfer/a-peek-behind-colossus-googles-file-system"
---

Most storage papers of the early 2000s tried to hide failure. GFS (Ghemawat, Gobioff & Leung, SOSP '03) opened by embracing it: with clusters of 1,000+ commodity machines, "component failures are the norm rather than the exception," so treat disks lying, machines dying, and bit rot as the steady state. Add three workload observations — files are huge (multi-GB), writes are overwhelmingly *appends*, and reads are large streams — and almost every strange-looking GFS decision falls out as a theorem from those axioms.

## One master, 64MB chunks, metadata in RAM

Files are split into **64MB chunks**, each identified by an immutable 64-bit handle and stored as a plain Linux file on **chunkservers**, replicated 3x by default. A **single master** holds all metadata: namespace, file→chunk mapping, and chunk→replica locations. The design's central bet is that this is safe because clients never move data through the master — they ask it for chunk locations, cache them, then talk to chunkservers directly. 64MB (huge for the era) is what makes the arithmetic work: fewer location lookups, long-lived client-chunkserver TCP connections, and **less than 64 bytes of metadata per chunk**, so the whole map fits in one machine's RAM.

Durability of that RAM image comes from a **write-ahead operation log** — the classic recipe from [WAL and crash recovery](/articles/distributed-systems/2026-08-10-write-ahead-log-wal) — replicated to remote machines before any mutation is acknowledged, plus periodic B-tree-shaped checkpoints so replay stays short. Chunk *locations* are deliberately not logged: the master rebuilds them by asking chunkservers at startup, because chunkservers have "the final word over what chunks it does or does not have" — a lesson in not maintaining a consistent view of state another component already owns. **Shadow masters** replay the log to serve slightly-stale reads when the primary is down; they're read-only spares, not failover consensus (this predates production Raft/Paxos-everywhere).

## Leases and the mutation pipeline

Concurrent writers need a mutation order, and the master must not be in the loop per-write. GFS grants a **60-second lease** (extended via heartbeat piggybacks) to one replica — the **primary** — which assigns serial numbers to all mutations on that chunk; the other replicas apply in that order. Leases give the standard time-based guarantee we covered in [leases as coordination](/articles/distributed-systems/2026-08-13-leases-time-based-coordination): even if the master loses contact with a primary, it can grant a new lease only after the old one *expires*, so two primaries never overlap.

The clever part is splitting **control flow from data flow**. Data is pushed linearly along a chain of replicas — each chunkserver forwards to the *nearest* replica not yet holding the data, pipelining as bytes arrive — to use each machine's full outbound bandwidth rather than fanning out from the client. Only after all replicas buffer the data does the client send the write request to the primary for ordering.

## Record append: at-least-once, and proud of it

The signature primitive is **record append**: the client supplies data, GFS chooses the offset. Hundreds of producers can append to the same file with no external locking — this is what fed MapReduce shuffles and made GFS files behave like multi-producer queues. The semantics are deliberately weak: GFS guarantees the record is written **atomically at least once** at some offset. If an append would straddle a chunk boundary, the primary **pads** the chunk and retries on a fresh one (appends are capped at ¼ of chunk size to bound waste). If any replica fails mid-append, the client retries, and replicas may hold **duplicates** — or region-level garbage where one replica succeeded and another didn't.

That yields the paper's famously relaxed consistency vocabulary: a region is **consistent** (all clients see the same bytes) and possibly **defined** (consistent *and* reflecting one mutation entirely). Serial writes are defined; concurrent overwrites are consistent but undefined (fragments interleave); failed mutations are inconsistent. GFS's answer to "isn't that horrible?" is co-design: applications self-delimit records with checksums, carry unique IDs to drop duplicates, and prefer append-then-rename-atomically. Push complexity into a library above the file system, keep the file system simple.

| Mutation | Guarantee | Client burden |
|---|---|---|
| Serial write | defined | none |
| Concurrent write | consistent, undefined | avoid; use appends |
| Record append | at-least-once, atomic per record | checksums + dedup IDs |
| Failed mutation | inconsistent until re-replicated | retry |

```text
record_append(file, data):
    chunk = master.last_chunk(file)           # cached; primary holds lease
    push data along replica chain             # data flow
    resp = primary.append(data)               # control flow: primary picks offset
    if resp == WOULD_STRADDLE:                # pad + retry on new chunk
        retry on next chunk
    if resp == REPLICA_FAILED:
        retry                                  # => possible duplicate records
    return offset                             # at least once, somewhere
```

## What broke, and what Colossus changed

The 2009 ACM Queue interview with Sean Quinlan (**"GFS: Evolution on Fast-forward"**) is the honest retrospective. As Google went "from a few hundred terabytes up to petabytes and then up to tens of petabytes," the single master's RAM and its one-namespace-mutex throughput became the wall — and 64MB chunks made file-*count* the binding constraint, since a million small files consume master memory like a few TB of big ones. Gmail-style serving workloads also couldn't tolerate the master's recovery pauses that batch jobs shrugged off.

**Colossus** kept chunkservers (now "D" file servers) but shattered the master into **curators** — horizontally scalable metadata services storing file system metadata **in Bigtable** (which itself runs on Colossus; the recursion bottoms out at bootstrap instances). Chunks shrank to ~1MB, background **custodians** handle repair and rebalancing, and replication gave way to Reed–Solomon encodings for cost — the technique from [erasure coding in object storage](/articles/distributed-systems/2026-08-13-erasure-coding-object-storage). Google cites 100x+ metadata scaling over the largest GFS cells, with single clusters at exabyte scale.

For the "design a distributed file system" interview, GFS remains the right skeleton: separate metadata from data, keep metadata in memory with a replicated log, lease out ordering, pipeline data along replica chains, and *choose* your consistency semantics to match the workload instead of paying for POSIX nobody needs. Then name the known cliff — the single master — and sketch the Colossus fix: shard the metadata over a scalable KV store.

**Try next:** read §2.7 and §3.3 of the paper, then write a single-machine simulation: three "replica" processes, one primary with an expiring lease, and a record-append client that randomly kills a replica mid-append. Count duplicate and padded records over 10,000 appends, then add client-side record IDs and verify your reader dedups to exactly-once.
