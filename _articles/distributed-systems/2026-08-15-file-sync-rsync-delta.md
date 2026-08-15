---
title: "Design File Sync (Dropbox) and the rsync Algorithm"
date: 2026-08-15
track: distributed-systems
summary: "File sync is content-addressed storage plus delta transfer plus a metadata journal. We work through rsync's two-level checksum trick (a rolling Adler-32-style weak hash filtering into a strong MD4/MD5 hash, ~700-byte blocks), why Dropbox instead ships fixed 4MB SHA-256 blocks and pipelines them, FastCDC's content-defined chunking (~10x faster than Rabin), and conflict handling — conflicted copies over last-writer-wins."
reading_time: 6
tags: [system-design, file-sync, rsync, rolling-hash, content-defined-chunking, deduplication]
sources:
  - title: "Tridgell, A. & Mackerras, P. — The rsync algorithm (ANU Technical Report TR-CS-96-05)"
    url: "https://rsync.samba.org/tech_report/"
  - title: "Dropbox Engineering — Streaming File Synchronization"
    url: "https://dropbox.tech/infrastructure/streaming-file-synchronization"
  - title: "Dropbox Engineering — Rewriting the Heart of Our Sync Engine"
    url: "https://dropbox.tech/infrastructure/rewriting-the-heart-of-our-sync-engine"
  - title: "Xia et al. — FastCDC: A Fast and Efficient Content-Defined Chunking Approach for Data Deduplication (USENIX ATC '16)"
    url: "https://www.usenix.org/system/files/conference/atc16/atc16-paper-xia.pdf"
---

You edit three bytes in the middle of a 2GB file. A naive sync client re-uploads 2GB; a good one uploads a few kilobytes. Everything interesting about "design Dropbox" lives in that gap, and the founding text is Tridgell and Mackerras's 1996 **rsync** technical report: how to compute a minimal delta between two files that sit on *opposite ends of a slow link*, where neither side can see both copies.

## The rsync algorithm: two checksums, one pass

The setup: receiver has an old copy B, sender has new copy A. Receiver splits B into fixed **blocks** (the report experiments around 300–1000 bytes; ~700 is the classic figure) and sends, per block, a **weak 32-bit rolling checksum** and a **strong 128-bit hash** (MD4 then; MD5/xxhash in modern rsync). The sender then slides a window over A *at every byte offset*, looking for blocks it already has:

```text
# receiver side
for each block b_i of B:
    send (i, weak(b_i), strong(b_i))

# sender side: 16-bit hash table keyed on weak checksum
w = weak(A[0 : L])
for k in 0 .. len(A) - L:
    if w in table and strong(A[k : k+L]) == table[w].strong:
        emit COPY(block_index)         # receiver reuses local block
        k += L; w = weak(A[k : k+L])   # jump a whole block
    else:
        emit LITERAL(A[k])             # raw byte
        w = roll(w, out=A[k], in=A[k+L])   # O(1) slide
```

The trick that makes per-byte matching affordable is the **rolling hash**, an Adler-32-style pair: `a = Σ X_i mod 2^16` and `b = Σ (L-i+1)·X_i mod 2^16`, combined as `s = a + 2^16·b`. Sliding the window one byte updates both in O(1) — subtract the outgoing byte, add the incoming one — instead of rehashing L bytes. The weak checksum is a cheap filter with false positives; the expensive **strong hash** confirms only candidate matches, and a three-level check (16-bit hash-table bucket → 32-bit weak → 128-bit strong) keeps the common miss path to a couple of instructions. Result: matches can occur at *any alignment*, so an insertion near the start of a file doesn't destroy every downstream match the way naive fixed-offset comparison would.

## Chunking: fixed blocks vs content-defined

rsync sidesteps the alignment problem by brute-force scanning every offset — fine for one file pair, wasteful for a storage system deduplicating billions of files. The storage-side alternative is to cut files so the *boundaries themselves* survive edits:

| | Fixed-size blocks | Content-defined chunking (CDC) |
|---|---|---|
| Boundary rule | every N bytes | where rolling hash mod D == r |
| Insert 1 byte early in file | shifts every later block → dedup lost | later boundaries unchanged |
| CPU cost | trivial | hash every byte (Rabin ≈ slow, Gear/FastCDC fast) |
| Chunk size variance | none | needs min/max clamps |
| Used by | Dropbox (4MB), rsync receiver | backup/dedup systems, restic, borg, LBFS lineage |

**FastCDC** (Xia et al., ATC '16) is the modern CDC reference: it replaces Rabin fingerprints with a **Gear hash** (one shift, one add, one table lookup per byte), judges boundaries on zero bits in the hash, and uses *normalized chunking* — a stricter mask before the target size, a looser one after — to tighten the size distribution while skipping the sub-minimum region entirely. The paper measures roughly **10x the chunking throughput of Rabin-based CDC** at equivalent dedup ratios, which matters because chunking is the front of every write path.

## Dropbox: content-addressed blocks and a journal

Dropbox's production answer is simpler than you'd guess: files are split into **fixed 4MB blocks, each named by its SHA-256 hash** — classic **content-addressed storage**. A file's metadata is its blocklist (ordered hash list); upload is "commit blocklist, server replies with the hashes it doesn't already have, client sends only those." That gives cross-user **dedup** for free (a popular file uploaded twice stores once) and makes small edits cheap — only the touched 4MB blocks change identity. Fixed blocks lose insert-shift resilience, but for typical desktop workloads (in-place edits, appends, whole-file rewrites) 4MB granularity captures most savings at a fraction of CDC's complexity.

Their **Streaming File Synchronization** work adds pipelining: instead of waiting for a full upload before peers can download, blocks stream through as they arrive, cutting sharer-to-sharee latency for large files from "upload time + download time" to roughly max of the two.

The part interviews underweight is **sync metadata**. The client keeps a local journal/database recording, per path: last-synced blocklist, mtime, size, and the server-assigned version/cursor. Sync is a three-way reconciliation among local disk state, journal, and remote state; the journal is what distinguishes "user edited the file" from "we never finished downloading it." Dropbox's 2020 **Nucleus** rewrite (Rust) exists precisely because their first engine let this state live in loosely coupled tables with implicit invariants — the rewrite made the local view, remote view, and synced snapshot three explicit trees, checked by randomized simulation testing, echoing the anti-entropy framing from [Merkle trees](/articles/distributed-systems/2026-07-27-merkle-trees-anti-entropy).

## Deltas, conflicts, and what to say in an interview

**Delta sync vs full upload** is a policy knob, not dogma: below ~4MB, just resend the file — checksum round-trips cost more than the bytes. Between block granularity (Dropbox) and byte granularity (rsync/librsync-style diffs against the previous version) the trade is server CPU and protocol round-trips against bandwidth.

**Conflicts:** two devices edit the same file offline. **Last-writer-wins** silently destroys one user's work using clocks you don't trust; every serious sync product instead forks — Dropbox writes `report (Bob's conflicted copy 2026-08-15).txt`, keeping both versions and delegating the merge to a human. Detection is compare-and-swap on the server: a commit names the parent version it built on; a stale parent means conflict, so rename-and-fork. That's the same optimistic-concurrency shape as [OCC vs 2PL](/articles/distributed-systems/2026-08-13-optimistic-concurrency-control-occ-2pl), applied to files.

Assemble the whole design: watcher (inotify/FSEvents) → chunker → content-addressed block store with dedup → metadata service holding blocklists and a per-account version cursor → notification channel telling other devices "cursor moved" → each device pulls the metadata delta and fetches missing blocks.

**Try next:** implement rsync's core in ~100 lines of Python: fixed 512-byte blocks, the a/b rolling checksum, MD5 as the strong hash. Generate a 10MB file, insert 3 bytes at offset 1000, and measure literal bytes transferred — then repeat with fixed-offset (non-rolling) matching and watch the delta balloon to nearly the whole file.
