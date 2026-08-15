---
title: "Cache Eviction Policies: FIFO to SIEVE, a Practical Tour of What to Throw Away"
date: 2026-08-10
track: sys-patterns
summary: "A cache is defined by what it forgets. A deep, practical tour of eviction policies — FIFO, LRU, LFU, CLOCK, 2Q, SLRU, ARC, LIRS, W-TinyLFU, and the 2023/24 upstarts S3-FIFO and SIEVE — framed around the recency/frequency tension and scan resistance. Includes the classic O(1) LRU interview implementation and a comparison table."
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

A cache is finite, so a cache is defined less by what it stores than by **what it decides to forget**. When the cache is full and a new item arrives, the eviction (or replacement) policy picks the victim. That single decision, made millions of times a second, sets your hit ratio — and a few points of hit ratio is the difference between a database that idles and one that melts.

This article is about **eviction**: given a full cache, what do you remove? A closely related question — *admission*, i.e. whether a new item deserves to enter at all — is covered in a [separate article on TinyLFU's doorkeeper](/articles/sys-patterns/2026-08-10-tinylfu-cache-admission-control/). Here I mention admission only where a modern policy (W-TinyLFU, S3-FIFO) blends the two.

## The tension: recency vs. frequency vs. scans

Every policy is a bet about the future encoded as a heuristic about the past. Two signals dominate:

- **Recency** — "recently used means soon-to-be-used." Great for temporal locality (a user paging through their own data). This is LRU's bet.
- **Frequency** — "often used means valuable." Great for a stable hot set (the top 1% of products that 50% of traffic wants). This is LFU's bet.

Neither alone is enough. Pure LRU is wrecked by a **scan**: one sequential pass over a large cold dataset (a batch job, a full-table backup) touches every item exactly once, and each touch shoves a genuinely hot item out of the cache. This is **cache pollution**, and resistance to it — **scan resistance** — is the property that separates toy policies from production ones. Pure LFU has the opposite failure: it clings to items that were hot last week and never lets a newly-popular item build up enough frequency to survive. The good modern policies **adapt** between recency and frequency, or use a small filter to keep scans out.

## The classics

**FIFO** evicts in insertion order — a plain queue, no per-access bookkeeping. Cheap and lock-free-friendly, but blind to reuse: a hot item inserted early gets evicted on schedule regardless of how often it's hit. Rarely the best hit ratio, but its simplicity is why the 2023–24 designs came back to it.

**LRU** evicts the least-recently-used item. Every access moves the item to the "most recent" end. It captures recency perfectly and is the industry default (Redis approximates it, Memcached uses it). Weaknesses: no frequency signal, and it is **not scan-resistant** — a scan flushes the working set. It also mutates shared state on *every read* (moving a node to the head), which is a lock contention headache under concurrency.

**LFU** evicts the least-frequently-used, keeping a counter per item. Strong on stable skewed workloads, but has three classic problems: stale counters (yesterday's hot item), slow adaptation, and the cost of maintaining a min-frequency structure. Practical LFUs need **aging** (periodically decay counts) to stay relevant.

### The O(1) LRU — the classic interview question

The interview task: `get` and `put` in O(1). The trick is a **hash map for lookup** plus a **doubly linked list for ordering**. The map gives O(1) find; the list gives O(1) move-to-front and O(1) tail eviction.

```python
class Node:
    __slots__ = ("key", "val", "prev", "next")
    def __init__(self, key=0, val=0):
        self.key, self.val = key, val
        self.prev = self.next = None

class LRUCache:
    def __init__(self, capacity: int):
        self.cap = capacity
        self.map = {}                      # key -> Node
        self.head, self.tail = Node(), Node()  # sentinels
        self.head.next = self.tail         # head <-> ... <-> tail
        self.tail.prev = self.head         # head side = MRU, tail side = LRU

    def _remove(self, node):
        node.prev.next, node.next.prev = node.next, node.prev

    def _add_front(self, node):            # insert right after head (MRU)
        node.prev, node.next = self.head, self.head.next
        self.head.next.prev = node
        self.head.next = node

    def get(self, key: int) -> int:
        if key not in self.map:
            return -1
        node = self.map[key]
        self._remove(node)                 # touch: promote to MRU
        self._add_front(node)
        return node.val

    def put(self, key: int, value: int) -> None:
        if key in self.map:
            self._remove(self.map[key])
        node = Node(key, value)
        self.map[key] = node
        self._add_front(node)
        if len(self.map) > self.cap:
            lru = self.tail.prev           # evict from tail
            self._remove(lru)
            del self.map[lru.key]
```

Sentinel head/tail nodes remove all the null-checking around empty and single-element cases — the detail interviewers look for. Note the write-on-read in `get`: that's exactly the property SIEVE later attacks.

## The smarter middle generation

**CLOCK / Second-Chance** is an approximation of LRU that avoids LRU's per-read list surgery. Items sit in a circular buffer, each with a **reference bit** set on access. A "hand" sweeps: if the bit is 1, clear it and give a second chance; if 0, evict. It's what OS page replacement actually uses because it needs no work on a hit beyond setting a bit — cheap and concurrency-friendly.

**2Q** (Johnson & Shasha) adds scan resistance with two queues: new items enter a small FIFO "in" queue; only items referenced *again* graduate to a main LRU queue. A one-shot scan dies in the FIFO and never pollutes the hot set. This "prove yourself on a second access" idea recurs everywhere below.

**SLRU** (Segmented LRU) splits the cache into a **probationary** and a **protected** segment. New items land in probation; a second hit promotes them to protected. Eviction always comes from probation first. Same principle as 2Q, and it's the internal structure of W-TinyLFU's main region.

**LIRS** (Jiang & Zhang, SIGMETRICS 2002) uses **reuse distance** (inter-reference recency) rather than plain recency, distinguishing "low reuse-distance" hot blocks from "high reuse-distance" cold ones. Excellent on looping/scan-heavy storage workloads, but the bookkeeping is intricate.

**ARC** (Megiddo & Modha, FAST '03) is the elegant one. It keeps **two LRU lists** — T1 for items seen once (recency) and T2 for items seen at least twice (frequency) — plus two **ghost lists** (B1, B2) that remember *keys of recently evicted items with no data*. A hit in a ghost list is a signal: "I evicted this too soon from the recency side (or frequency side)." ARC uses that signal to **adaptively move a target boundary `p`**, shrinking one list and growing the other. It self-tunes between recency and frequency with no magic constants, is scan-resistant, and costs only ~2× the metadata of LRU. (Its main real-world friction is that IBM held patents on it — a reason open-source systems often reached for alternatives.)

## The 2023–24 rethink: FIFO is enough

**W-TinyLFU** (the policy behind [Caffeine](https://www.mintlify.com/ben-manes/caffeine/advanced/efficiency)) combines a tiny LRU **window** (~1% of capacity) with a large **main** region managed by SLRU and guarded by **TinyLFU admission**. Frequency is estimated by a **4-bit Count-Min sketch** (~2 bytes/entry) that is **periodically halved** to age out stale popularity. On a main-region eviction, the incoming candidate and the victim are compared by estimated frequency — *higher frequency wins*. The small window captures bursts and recency; the frequency filter keeps one-hit scan items out of the main region. The result: hit ratios within a few percent of Belady's optimal on many traces, well above plain LRU, at O(1) cost.

**S3-FIFO** (Yang, Zhang, Qiu, Yue & Vinayak, SOSP '23 — *"FIFO Queues Are All You Need for Cache Eviction"*) throws out linked-list LRU entirely and uses **three FIFO queues**: a **small** queue (~10%) that filters new arrivals, a **main** queue (~90%), and a **ghost** queue of evicted keys. The insight is **quick demotion**: in skewed workloads most objects are one-hit wonders, so evict them fast. An item in the small queue that gets a second hit is promoted to main; otherwise it's demoted (its key parked in the ghost). A later hit on a ghost key means "should've kept it" and admits it straight to main. FIFO queues are far more scalable than LRU (the paper reports ~6× throughput at 16 threads) *and* often a better hit ratio, because quick demotion is exactly what scan-heavy, skewed traffic wants.

**SIEVE** (Zhang, Yang, Yue, Vigfusson & Rashmi, NSDI '24 — *"SIEVE is Simpler than LRU"*) is almost embarrassingly simple. It's a single FIFO queue where each object has one **visited bit**. A moving **hand** pointer sweeps from tail toward head: visited bit set → clear it and skip (a second chance); bit clear → evict. New items are always inserted at the head, and — crucially — **a hit only sets the bit, it never moves the object** (lazy promotion). That means cache hits require **no list mutation and no locking**, the exact opposite of LRU's write-on-read. Despite the simplicity, SIEVE matches or beats far more complex policies on web-cache traces and is trivial to drop into an existing FIFO/LRU codebase.

## Comparison table

| Policy | Signal | Scan-resistant | Adapts recency/freq | Cost / metadata | Best on |
|---|---|---|---|---|---|
| FIFO | insertion order | No | No | Very low | Simplicity, streaming |
| LRU | recency | No | No | O(1), write-on-read | Temporal locality |
| LFU (+aging) | frequency | Partly | No | Min-freq structure | Stable hot set |
| CLOCK | approx recency | No | No | 1 bit/item | OS pages, low overhead |
| 2Q / SLRU | recency + reuse | Yes | Weakly | ~2 queues | General workloads |
| LIRS | reuse distance | Yes | Partly | Higher, intricate | Storage / loops |
| ARC | recency + freq | Yes | **Yes** (ghosts) | ~2× LRU | Mixed, self-tuning |
| W-TinyLFU | freq sketch + LRU window | Yes | Yes | ~2 B/entry sketch | Skewed web/app caches |
| S3-FIFO | freq-of-2 via FIFOs | Yes | Yes | 3 FIFOs + ghost | Skewed, high-concurrency |
| SIEVE | 1 visited bit + hand | Yes | Weakly | 1 bit/item, lock-free hits | Web caches, turn-key |

## What to actually reach for

If you need a default that is simple, concurrent, and hard to beat on skewed traffic, **SIEVE** or **S3-FIFO** are the current sweet spot — FIFO-based, lock-light, scan-resistant. If you're on the JVM, **Caffeine's W-TinyLFU** is the mature, battle-tested choice. **ARC** remains the textbook example of principled self-tuning. And plain **LRU** is still fine when locality is strong and scans are absent — just know that the moment a batch job walks your keyspace, its hit ratio falls off a cliff.

**Try next:** run your own production trace through the open-source [libCacheSim](https://github.com/1a1a11a/libCacheSim) simulator and compare LRU vs. SIEVE vs. W-TinyLFU hit ratios — then re-read the [TinyLFU admission](/articles/sys-patterns/2026-08-10-tinylfu-cache-admission-control/) article to see how *admission* control stacks on top of the eviction policy you just picked.
