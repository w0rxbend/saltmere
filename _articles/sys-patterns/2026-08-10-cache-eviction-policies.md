---
title: "Cache Eviction Policies: From FIFO to SIEVE"
date: 2026-08-10
track: sys-patterns
summary: "A cache is defined by what it forgets. A survey of eviction policies — FIFO, LRU, LFU, CLOCK, 2Q, SLRU, ARC, LIRS, W-TinyLFU, and the 2023/24 designs S3-FIFO and SIEVE — framed around the recency/frequency tension, scan resistance, and the cost each policy imposes on the hit path. Includes a comparison table."
reading_time: 7
tags: [caching, eviction, lru, lfu, arc, w-tinylfu, s3-fifo, sieve, algorithms, interview-prep]
sources:
  - title: "Megiddo & Modha — ARC: A Self-Tuning, Low Overhead Replacement Cache (USENIX FAST '03)"
    url: "https://www.usenix.org/legacy/event/fast03/tech/full_papers/megiddo/megiddo.pdf"
  - title: "Einziger, Friedman & Manes — TinyLFU: A Highly Efficient Cache Admission Policy (arXiv:1512.00727 / ACM ToS 2017)"
    url: "https://arxiv.org/abs/1512.00727"
  - title: "Yang, Zhang, Qiu, Yue & Vinayak — FIFO Queues Are All You Need for Cache Eviction (SOSP '23)"
    url: "https://www.pdl.cmu.edu/ftp/Storage/FIFOqueues-SOSP23_abs.shtml"
  - title: "Zhang, Yang, Yue, Vigfusson & Rashmi — SIEVE is Simpler than LRU (USENIX NSDI '24)"
    url: "https://www.usenix.org/conference/nsdi24/presentation/zhang-yazhuo"
  - title: "Caffeine — W-TinyLFU Eviction Policy (efficiency docs)"
    url: "https://www.mintlify.com/ben-manes/caffeine/advanced/efficiency"
---

**Gist.** A cache of capacity *C* must choose a victim every time a miss arrives at a full cache, and that choice — not the storage — determines the hit ratio. Every deployed policy approximates Belady's optimal offline rule (evict the item whose next reference is furthest in the future, unimplementable online) with a heuristic over past references: recency, frequency, or a blend. The cost of the heuristic is metadata per entry plus, in the case of least-recently-used (LRU), a **mutation of shared state on every read**, which converts a read-mostly workload into a write-mostly one at the lock.

The complementary question — *admission*, whether an arriving item deserves entry at all — is treated in a [separate article on TinyLFU's doorkeeper](/articles/sys-patterns/2026-08-10-tinylfu-cache-admission-control/), and appears below only where a policy blends the two.

## The tension: recency, frequency, scans

Each policy encodes a prediction about future references as a statistic over past ones. Two signals dominate.

- **Recency** — the reuse-distance distribution has mass near zero, so a recently referenced item is likely to be referenced again. This is LRU's bet.
- **Frequency** — under a Zipf-like popularity distribution with skew α ≈ 0.8–1.0, a small head of the keyspace absorbs most requests, so long-run reference count predicts value. This is least-frequently-used (LFU)'s bet.

Neither is sufficient, and the failure modes are complementary. LRU is destroyed by a **scan**: a sequential pass over *N* > *C* distinct cold items references each exactly once, and since every reference is a miss that inserts at the most-recently-used end, the entire working set is displaced. **The number of hot entries surviving a scan of length N is max(0, C − N), so any scan of length ≥ C empties the cache** — hit ratio drops to zero and recovers only over a full re-warm. Resistance to this property is termed **scan resistance**. LFU fails in the opposite direction: an item with a large accumulated count is unevictable long after its popularity ends, and a newly popular item cannot accumulate enough count to displace it. Practical LFU therefore requires **aging** — periodic halving or windowed decay of counters.

## The classics

**FIFO** (first-in, first-out) evicts in insertion order. Per-hit cost is zero: hits touch no shared state, so the structure is trivially concurrent. It is blind to reuse — an item inserted early is evicted on schedule regardless of reference count — and its hit ratio is normally the worst of the group. The 2023–24 designs build on this FIFO base, adding at most a single bit to the hit path.

**LRU** evicts the least-recently-used entry and gives O(1) `get` and `put` via a hash map for lookup plus a doubly linked list for ordering: the map yields O(1) find, the list yields O(1) unlink and O(1) splice to the head, and the tail is the victim. Sentinel head and tail nodes remove every null check for the empty and single-element cases. Two structural weaknesses follow from the design: there is no frequency signal, and the promotion on `get` is a **write on the read path**, so under concurrency every hit contends for the list lock. Redis approximates LRU by sampling (`maxmemory-samples`, default 5 candidates) precisely to avoid maintaining the list at all.

**LFU** keeps a per-entry counter and needs a structure supporting O(1) increment and O(1) minimum — typically buckets of equal-count entries in a linked list of frequencies. Its costs are counter memory, bucket bookkeeping, and the staleness above.

## The middle generation

**CLOCK / second chance** approximates LRU with **one reference bit per entry** and no list surgery. Entries occupy a circular buffer; a hit sets the bit. A hand sweeps: bit = 1 → clear it and advance (second chance); bit = 0 → evict. The state machine per entry is `{resident,1} → {resident,0} → evicted`, with any hit resetting to `{resident,1}`. Amortised sweep cost is O(1); worst case is one full revolution when every bit is set. Operating-system page replacement uses this because the hit path is a single bit store.

**2Q** (Johnson & Shasha, VLDB 1994) adds scan resistance with two structures: arrivals enter a small FIFO, and **only a second reference promotes an entry to the main LRU queue**. A one-shot scan therefore dies in the FIFO. **SLRU** (segmented LRU) expresses the same invariant as a probationary and a protected segment with eviction always drawn from probation; it is the internal structure of W-TinyLFU's main region.

**LIRS** (Jiang & Zhang, SIGMETRICS 2002) ranks by **reuse distance** — the count of distinct items referenced between two references to the same item — rather than by recency. It is strong on looping storage workloads; its stack-pruning bookkeeping is intricate.

**ARC** (Megiddo & Modha, USENIX FAST '03) maintains two LRU lists, T1 for items seen once and T2 for items seen at least twice, plus two **ghost lists** B1 and B2 holding only the keys of recently evicted entries, no data. The invariant is |T1| + |T2| ≤ C and |T1| + |B1| ≤ C. A hit in B1 proves the recency side was starved and increases the target size `p` for T1; a hit in B2 increases T2's share. **ARC self-tunes with no workload-specific constant, is scan-resistant because a single-reference item never enters T2 — it is evicted from T1 into the ghost list B1 without ever displacing the frequency-side working set — and costs roughly 2× LRU's metadata** for the ghost keys. ARC is covered by IBM patents.

## The 2023–24 rethink: FIFO suffices

**W-TinyLFU**, the policy in [Caffeine](https://www.mintlify.com/ben-manes/caffeine/advanced/efficiency), places a small LRU **window** (1 % of capacity by default, resized at runtime by Caffeine's hill-climbing adaptation) ahead of an SLRU **main** region guarded by TinyLFU admission. Frequency is estimated by a **4-bit Count–Min sketch sized to the cache's maximum entry count**, four counters consulted per key, halved once a sample-size threshold of increments is reached so stale popularity decays geometrically. On eviction the candidate and the victim are compared by estimated frequency and the higher wins. Caffeine's efficiency documentation reports hit ratios close to Belady's optimum on several traces at O(1) per-operation cost.

**S3-FIFO** (Yang et al., SOSP '23) discards linked-list LRU for three FIFO queues: **small** (≈10 % of capacity), **main** (≈90 %), and a ghost queue of evicted keys. The load-bearing idea is **quick demotion**: on skewed traces most objects are one-hit wonders, so evicting them within one small-queue traversal preserves the main queue. A second hit in small promotes to main; a ghost hit admits directly to main. The paper reports approximately **6× throughput at 16 threads relative to LRU** alongside an equal or better hit ratio.

**SIEVE** (Zhang et al., USENIX NSDI '24) is a single FIFO with one **visited bit** per object and a hand that moves from tail toward head, skipping and clearing set bits and evicting the first clear one. Insertions go to the head; the hand does not reset to the tail on insertion, which is what distinguishes SIEVE from CLOCK. Crucially **a hit only sets a bit — the object is never moved — so the hit path performs no list mutation and needs no lock**, the inverse of LRU's write-on-read.

### Implementation sketch (Scala)

```scala
final class Sieve[K, V](capacity: Int):
  private final class Node(val key: K, var value: V):
    var visited: Boolean = false
    var prev, next: Node = null          // head side = newest

  private val index = scala.collection.mutable.HashMap.empty[K, Node]
  private var head, tail, hand: Node = null

  def get(key: K): Option[V] = index.get(key).map { n =>
    n.visited = true                     // the entire hit path: one bit store
    n.value
  }

  def put(key: K, value: V): Unit = index.get(key) match
    case Some(n) => n.value = value; n.visited = true
    case None =>
      if index.size >= capacity then evict()
      val n = Node(key, value)
      n.next = head
      if head != null then head.prev = n else tail = n
      head = n
      index(key) = n

  private def evict(): Unit =
    var c = if hand != null then hand else tail
    while c.visited do                   // second chance, then move toward head
      c.visited = false
      c = if c.prev != null then c.prev else tail
    hand = c.prev                        // hand survives across evictions
    unlink(c)
    index -= c.key

  private def unlink(n: Node): Unit =
    if n.prev != null then n.prev.next = n.next else head = n.next
    if n.next != null then n.next.prev = n.prev else tail = n.prev
```

The loop terminates because each iteration clears one bit and no bit is set inside `evict`, bounding the sweep by the number of resident entries.

## Comparison table

| Policy | Signal | Scan-resistant | Adapts recency/freq | Cost / metadata | Best on |
|---|---|---|---|---|---|
| FIFO | insertion order | No | No | Very low | Simplicity, streaming |
| LRU | recency | No | No | O(1), write-on-read | Temporal locality |
| LFU (+aging) | frequency | No | No | Min-freq structure | Stable hot set |
| CLOCK | approx recency | No | No | 1 bit/item | OS pages, low overhead |
| 2Q / SLRU | recency + reuse | Yes | Weakly | ~2 queues | General workloads |
| LIRS | reuse distance | Yes | Partly | Higher, intricate | Storage / loops |
| ARC | recency + freq | Yes | **Yes** (ghosts) | ~2× LRU | Mixed, self-tuning |
| W-TinyLFU | freq sketch + LRU window | Yes | Yes | 4-bit sketch + window | Skewed web/app caches |
| S3-FIFO | freq-of-2 via FIFOs | Yes | Yes | 3 FIFOs + ghost | Skewed, high-concurrency |
| SIEVE | 1 visited bit + hand | Yes | Weakly | 1 bit/item, no list mutation on hits | Web caches |

## Pitfalls

- A batch job or backup that walks the keyspace collapses an LRU cache's hit ratio to near zero for the duration plus the re-warm period, because every scanned item is a miss inserted at the most-recently-used end.
- An LFU without aging serves last week's hot set indefinitely: counters are monotonic, so a decayed item still outranks any newcomer that has not accumulated the same count.
- LRU under concurrency degrades to a single-writer structure, because the promotion in `get` mutates the shared list and therefore takes the same lock the writes take.
- Sizing a ghost list smaller than the cache breaks ARC's adaptation: with |T1| + |B1| < C the "evicted too soon" signal is discarded before it can arrive, and `p` stops moving.
- Sizing W-TinyLFU's window at zero rejects bursty new content outright, since an item with sketch estimate 0 loses the admission comparison against any resident victim whose estimate is non-zero.
- Resetting SIEVE's hand to the tail on each eviction turns it into CLOCK and forfeits the property that the hand partitions old from newly inserted objects.
- Measuring policies on a uniform-random trace leaves almost nothing to separate them, because every reference is equally likely and no past-reference statistic predicts the next one.

**Further work:** replaying a production trace through [libCacheSim](https://github.com/1a1a11a/libCacheSim) compares LRU, SIEVE and W-TinyLFU at the deployed capacity.
