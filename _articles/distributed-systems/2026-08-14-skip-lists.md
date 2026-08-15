---
title: "Skip Lists: Ordered Data Without a Balanced Tree"
date: 2026-08-14
track: distributed-systems
summary: "A skip list keeps elements sorted with expected O(log n) search and insert using linked lists and coin flips — no rotations, no rebalancing. How the levels work, an implementation sketch, and why Redis, RocksDB and the JVM ship it."
reading_time: 6
tags: [data-structures, skip-list, redis, rocksdb, concurrency]
sources:
  - title: "Pugh, W. — Skip Lists: A Probabilistic Alternative to Balanced Trees (CACM, 1990)"
    url: "https://15721.courses.cs.cmu.edu/spring2018/papers/08-oltpindexes1/pugh-skiplists-cacm1990.pdf"
  - title: "Redis sorted sets — internal encoding (skip list + hash table)"
    url: "https://redis.io/docs/latest/develop/data-types/sorted-sets/"
  - title: "redis/src/t_zset.c — zsl* skip list implementation"
    url: "https://github.com/redis/redis/blob/unstable/src/t_zset.c"
  - title: "RocksDB Wiki — MemTable (skiplist is the default, supports concurrent insert)"
    url: "https://github.com/facebook/rocksdb/wiki/MemTable"
  - title: "ConcurrentSkipListMap (Java SE 17 API)"
    url: "https://docs.oracle.com/en/java/javase/17/docs/api/java.base/java/util/concurrent/ConcurrentSkipListMap.html"
---

**Gist.** Ordered data with logarithmic operations conventionally requires a balanced search tree, whose rotations, height bookkeeping and split/merge cases must all be implemented correctly. A **skip list** obtains the same asymptotics from a sorted linked list plus a random height per node, so no global rebalancing step exists. The cost is that the bounds are **expected rather than worst case**: an unlucky sequence of coin flips degrades an operation toward O(n), and each node carries one forward pointer per level it occupies.

William Pugh introduced the structure in 1990 as *"a probabilistic alternative to balanced trees"*. It is the index inside Redis sorted sets, the default RocksDB memtable, and the backing structure of Java's `ConcurrentSkipListMap`.

## Express lanes over a linked list

The starting point is an ordinary sorted singly linked list. Search is O(n) because every node between the head and the target must be visited. A skip list adds *express lanes*: a second list that skips over some nodes, a third that skips over more, and so on. **Every node appears at level 1; a node appears at level k+1 only if it appears at level k**, so the levels are nested sublists.

Search starts at the highest occupied level of the head node and follows two rules: move right while the next key on the current lane is smaller than the target, and drop one level when moving right would overshoot. High lanes cover distance; the bottom lane supplies precision. The search terminates at the immediate predecessor of the target on level 1, and a single step right decides membership.

Node height is not computed from the current shape of the structure — it is drawn from a geometric distribution. On insert, a node starts at level 1 and grows one level while a biased coin comes up heads with probability `p`, typically `1/4` or `1/2`. **That is the entire balancing strategy: no node is ever moved, and no existing pointer is rewritten except the ones adjacent to the insertion point.** Pugh's paper derives an expected number of levels of `log_1/p(n)` and expected search cost O(log n).

## The predecessor array is the load-bearing detail

Insert performs exactly the same descent as search, but records, for each level, **the last node visited on that level** — the node whose forward pointer at that level would have to change if the new node were spliced in there. Pugh calls this the `update` array. Once the random height is drawn, the splice is a fixed sequence per level: the new node's forward pointer at level i takes the value of `update[i]`'s forward pointer at level i, and `update[i]`'s pointer is redirected to the new node.

Two invariants make the descent correct. First, **`update[i].key < newKey` for every level i**, because the descent only moves right while the next key is strictly smaller. Second, **`update[i]` is at or to the right of `update[i+1]`**, since level i is a superset of level i+1 and the walk resumes from where the higher level stopped. Violating either — for instance by resetting the cursor to the head on each level instead of continuing from the previous level's stopping point — produces a list that still searches correctly at level 1 while the express lanes point past the new node, so the structure degrades silently.

Deletion is the mirror operation: run the same descent, then for each level where `update[i]`'s successor is the victim, redirect that pointer past it. Levels above the victim's height are untouched. When the topmost lanes become empty, the recorded list height is decremented; leaving it high wastes one pointer dereference per search but does not break correctness.

## Why production systems use it

**Locality under concurrency.** No operation reshapes a subtree, so an insert rewrites a bounded set of forward pointers around one position. `ConcurrentSkipListMap` in the Java platform library is a concurrent variant of the structure whose API documentation states an expected average cost of log(n) for `containsKey`, `get`, `put` and `remove`, with no locking on the map as a whole. The RocksDB wiki records that the default skiplist memtable is the only memtable type supporting concurrent inserts.

**Range scans require no extra structure.** Level 1 is a fully sorted linked list, so a range query is one search plus a linear walk along the bottom lane. Redis gives sorted sets (ZSETs) two encodings and uses the skiplist encoding once a set exceeds the configured size thresholds; that encoding pairs a skip list with a hash table. The hash table answers member-to-score lookups in O(1), and the skip list answers rank and range queries such as `ZRANGEBYSCORE` in O(log n) plus the size of the result. Log-structured merge-tree (LSM-tree) stores including RocksDB use the skip list as the in-memory memtable, so the data is already in sorted order when it is flushed to a sorted string table (SSTable).

The word *expected* carries the trade-off. A run of coin flips that gives many nodes height 1 leaves long stretches with no express lane over them, and the affected searches approach O(n). Pugh's remedy is not a repair step but a parameter choice: fix `p` and cap the height at roughly `log_1/p(N)` for the anticipated element count N. No published measurement from the systems named above separates their skip lists from a balanced tree under contention.

### Implementation sketch (Scala)

```scala
final class SkipList[K](maxLevel: Int = 16, p: Double = 0.5)(using ord: Ordering[K]):
  private final class Node(val key: K, height: Int):
    val forward: Array[Node] = new Array[Node](height)   // entries start null

  private val head = Node(null.asInstanceOf[K], maxLevel)
  private var level = 1                       // count of occupied lanes; indices are 0-based

  private def randomLevel(): Int =
    var lvl = 1
    while lvl < maxLevel && scala.util.Random.nextDouble() < p do lvl += 1
    lvl

  /** Descend, recording the last node visited on each lane. */
  private def descend(key: K): Array[Node] =
    val update = Array.fill(maxLevel)(head)
    var node = head
    var i = level - 1
    while i >= 0 do
      var nxt = node.forward(i)
      while nxt != null && ord.lt(nxt.key, key) do
        node = nxt                           // cursor is NOT reset between lanes
        nxt = node.forward(i)
      update(i) = node
      i -= 1
    update

  def contains(key: K): Boolean =
    val succ = descend(key)(0).forward(0)
    succ != null && ord.equiv(succ.key, key)

  def insert(key: K): Unit =
    val update = descend(key)
    val lvl = randomLevel()
    if lvl > level then level = lvl          // new lanes start at head
    val fresh = Node(key, lvl)
    var i = 0
    while i < lvl do
      fresh.forward(i) = update(i).forward(i)
      update(i).forward(i) = fresh
      i += 1
```

## Pitfalls

- Resetting the search cursor to the head node on each level instead of continuing from the previous level's stopping node still yields correct results, because level 1 remains sorted, but turns the descent into a per-level linear scan and erases the logarithmic bound.
- Drawing the random height from a source correlated with the key — a hash of the key, for example — makes the height distribution deterministic per key set, so adversarial or merely unlucky key sets produce lanes that never cover the hot range.
- Growing the recorded list height without initialising the newly occupied entries of the predecessor array to the head node leaves those lanes spliced onto stale predecessors, which drops every node inserted before them out of the upper lanes.
- Deleting a node without shrinking the recorded height when the top lanes empty leaves searches starting on lanes containing no nodes; the result stays correct while each search pays extra dereferences.
- Treating the O(log n) bound as a worst case in latency budgeting misstates the guarantee: the bound is expected, and the tail is set by the coin, not by the input order.
- Sizing the maximum level far below `log_1/p(N)` caps the express lanes, so beyond N elements the top lane itself becomes a linear scan.
