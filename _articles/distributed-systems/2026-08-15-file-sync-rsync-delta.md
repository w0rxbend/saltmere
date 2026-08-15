---
title: "Design File Sync (Dropbox) and the rsync Algorithm"
date: 2026-08-15
track: distributed-systems
summary: "File sync is content-addressed storage plus delta transfer plus a metadata journal. This article works through rsync's two-level checksum scheme (a rolling Adler-32-style weak hash filtering into a strong MD4/MD5 hash, ~700-byte blocks), Dropbox's fixed 4MB SHA-256 blocks and their pipelined transfer, FastCDC's content-defined chunking (~10x the chunking throughput of Rabin), and conflict handling by conflicted copies rather than last-writer-wins."
reading_time: 7
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

**Gist.** Editing three bytes in the middle of a 2GB file must not cost 2GB of upload, and neither endpoint of a slow link can see both copies of the file. The founding answer is Tridgell and Mackerras's 1996 **rsync** technical report: the receiver describes its old copy with per-block checksums, and the sender scans its new copy at **every byte offset** for blocks the receiver already holds, emitting copy-references instead of data. The cost is a full per-byte hash pass over the sender's file plus a round trip carrying the receiver's block signatures — which is why production systems such as Dropbox trade some of rsync's alignment resilience for coarse fixed blocks that can be addressed by content hash and deduplicated globally.

## The rsync algorithm: two checksums, one pass

The receiver holds old copy B, the sender holds new copy A. The receiver splits B into fixed **blocks** (the report works through its example with a block size of 700 bytes) and sends, per block, a **weak 32-bit rolling checksum** and a **strong 128-bit hash** (MD4 in the report; MD5 and other strong hashes in modern rsync). The sender then slides a window of length L over A:

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

Per-byte matching is affordable because of the **rolling hash**, an Adler-32-style pair: `a = Σ X_i mod 2^16` and `b = Σ (L-i+1)·X_i mod 2^16`, combined as `s = a + 2^16·b`. Advancing the window by one byte updates both in O(1) — subtract the outgoing byte's contribution, add the incoming one — rather than rehashing L bytes, so the whole scan is O(len(A)) rather than O(len(A)·L).

The weak checksum is a filter that admits false positives; the **strong hash is the arbiter**, computed only for candidates. The report's three-level check — 16-bit hash-table bucket, then 32-bit weak checksum, then 128-bit strong hash — keeps the common case, a miss, to a small constant number of instructions. The invariant that makes the protocol correct is that **a COPY is emitted only after strong-hash equality**, so the reconstructed file matches A unless the strong hash collides.

The payoff of scanning every offset is alignment independence: **matches may begin at any byte position**, so inserting bytes near the start of a file does not destroy the matches downstream, as fixed-offset comparison would.

## Chunking: fixed blocks versus content-defined

rsync solves alignment by brute force at transfer time. A storage system deduplicating billions of files cannot pay that per pair, so the alternative is to cut files such that the **boundaries themselves are determined by content** and therefore survive edits elsewhere.

| | Fixed-size blocks | Content-defined chunking (CDC) |
|---|---|---|
| Boundary rule | every N bytes | where rolling hash mod D == r |
| Insert 1 byte early in file | shifts every later block → dedup lost | later boundaries unchanged |
| CPU cost | trivial | hash every byte (Rabin slower, Gear/FastCDC faster) |
| Chunk size variance | none | needs min/max clamps |
| Used by | Dropbox (4MB), rsync receiver | backup/dedup systems, restic, borg, LBFS lineage |

**FastCDC** (Xia et al., ATC '16) is the modern CDC reference. It replaces Rabin fingerprints with a **Gear hash** — one shift, one addition and one table lookup per byte — declares a boundary where the masked bits of the hash are zero, and applies *normalized chunking*: a stricter mask before the target chunk size and a looser mask after it, which narrows the chunk-size distribution. It also **skips the sub-minimum region entirely** rather than hashing bytes that cannot legally end a chunk. The paper measures roughly **10x the chunking throughput of Rabin-based CDC while achieving nearly the same deduplication ratio**, which matters because chunking sits at the front of every write path.

## Dropbox: content-addressed blocks and a journal

Dropbox splits files into **fixed 4MB blocks, each named by its SHA-256 hash** — content-addressed storage. A file's metadata is its blocklist, the ordered list of block hashes. Upload commits a blocklist; the server replies with the hashes it does not already hold, and the client sends only those. Two consequences follow directly: **deduplication is a property of naming**, since identical content produces identical names regardless of which account uploaded it, and a small edit changes the identity only of the 4MB blocks it touches. Fixed blocks forfeit insert-shift resilience; the granularity still captures savings for in-place edits, appends and whole-file rewrites without CDC's per-byte hashing.

The **Streaming File Synchronization** work pipelines the transfer: rather than a peer waiting for the whole upload to complete before downloading, blocks are forwarded as they arrive, reducing sharer-to-sharee latency for large files from upload time plus download time to approximately the larger of the two.

The component that carries the correctness burden is **sync metadata**. The client keeps a local journal recording, per path, the last-synced blocklist, mtime, size, and the server-assigned version cursor. Synchronisation is a three-way reconciliation among local disk state, journal, and remote state, and **the journal is the only thing that distinguishes "the user edited this file" from "a download never finished"**: without it, a partially written local file is indistinguishable from a local edit and is uploaded as truth. Dropbox's Nucleus rewrite (in Rust) makes the local view, the remote view and the synced snapshot three explicit trees, checked by randomized simulation testing — the same anti-entropy framing as [Merkle trees](/articles/distributed-systems/2026-07-27-merkle-trees-anti-entropy).

## Deltas and conflicts

Delta transfer versus full upload is a policy threshold rather than a rule: for files near or below a single block, the checksum round trip costs more than the bytes it saves. Between block granularity (Dropbox) and byte granularity (rsync- or librsync-style diffs against the previous version) the trade is server CPU and protocol round trips against bandwidth.

Conflicts arise when two devices edit the same file while offline. **Last-writer-wins discards one user's work and depends on clocks the system does not control.** Dropbox instead forks, writing a file such as `report (Bob's conflicted copy 2026-08-15).txt` and leaving the merge to a human. Detection is compare-and-swap on the server: **a commit names the parent version it was built on, and a parent that is no longer current means a conflict**, so the server rejects the overwrite and the client renames. This is the optimistic-concurrency shape described in [OCC vs 2PL](/articles/distributed-systems/2026-08-15-optimistic-vs-pessimistic-concurrency-control), applied to files.

The assembled design: filesystem watcher (inotify, FSEvents) → chunker → content-addressed block store with deduplication → metadata service holding blocklists and a per-account version cursor → notification channel announcing that the cursor moved → each device pulls the metadata delta and fetches the blocks it lacks.

### Implementation sketch (Scala)

The rolling checksum and the two-level match test, on which the whole delta depends:

```scala
final case class Sig(index: Int, weak: Int, strong: Vector[Byte])

/** a = Σ X_i, b = Σ (L-i+1)·X_i, both mod 2^16; s = a + 2^16·b. */
final class Rolling(data: Array[Byte], length: Int):
  private var a, b = 0
  private var start = 0

  def init(from: Int): Int =
    start = from; a = 0; b = 0
    var i = from
    while i < from + length do
      a = (a + (data(i) & 0xff)) & 0xffff
      b = (b + (from + length - i) * (data(i) & 0xff)) & 0xffff
      i += 1
    a | (b << 16)

  /** O(1) advance: drop data(start), admit data(start + length). */
  def roll(): Int =
    val out = data(start) & 0xff
    val in = data(start + length) & 0xff
    a = (a - out + in) & 0xffff
    b = (b - length * out + a) & 0xffff   // b absorbs the already-updated a
    start += 1
    a | (b << 16)

def matchAt(table: Map[Int, Sig], weak: Int, window: Array[Byte]): Option[Int] =
  table.get(weak)                                   // weak checksum is a filter,
    .filter(_.strong == strongHash(window))         // the strong hash decides
    .map(_.index)
```

## Pitfalls

- Comparing only the weak rolling checksum emits a COPY for a block that differs, and the receiver reconstructs a file that is not A; the weak checksum has false positives by construction and is a filter, not a decision.
- Restarting the rolling hash from scratch after every literal byte turns the O(len(A)) scan into O(len(A)·L); the hash must be advanced with the outgoing/incoming byte pair, and only reinitialised after a block-length jump.
- With fixed-size blocks, inserting a single byte near the start of a file changes the content of every subsequent block, so a client uploads the whole file even though almost all bytes are unchanged.
- Content-defined chunking without minimum and maximum clamps produces pathological chunk sizes on inputs whose rolling hash rarely or constantly hits the boundary condition, such as long runs of identical bytes.
- Treating filesystem mtime as the sync state, without the journal's last-synced blocklist, makes a half-written downloaded file indistinguishable from a local edit and propagates the truncated content to every device.
- Resolving offline edits by last-writer-wins deletes the loser's content permanently and does so according to clocks the sync service does not control; a conflicted-copy fork preserves both versions.
- Committing a blocklist without naming the parent version removes the compare-and-swap on which conflict detection rests, so a stale client silently overwrites a newer server version.
