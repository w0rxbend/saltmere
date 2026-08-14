---
title: "Skip Lists: Ordered Data Without a Balanced Tree"
date: 2026-08-14
track: distributed-systems
summary: "A skip list keeps elements sorted with expected O(log n) search and insert using nothing but linked lists and coin flips — no rotations, no rebalancing. Here is how the levels work, a ~30-line Python implementation, and why Redis, RocksDB, and the JVM all reach for it."
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

A balanced tree gives you sorted data with O(log n) operations, but the machinery is fiddly: red-black rotations, AVL height tracking, split/merge cases you have to get exactly right. A **skip list** buys the same asymptotics out of two much simpler ingredients — a sorted linked list and a coin. William Pugh introduced it in 1990 precisely as *"a probabilistic alternative to balanced trees"*, and three decades later it is the index inside Redis sorted sets, the default RocksDB memtable, and Java's `ConcurrentSkipListMap`.

## The idea: express lanes over a linked list

Start with an ordinary sorted singly linked list. Searching it is O(n) because you must walk every node. Now add *express lanes*: a second list that skips over some nodes, a third that skips over more, and so on. Each node exists at level 1; some also appear at level 2, fewer at level 3. To search, you start at the top-left, move right while the next key is smaller than your target, and drop down a level when it would overshoot. High levels cover ground fast; low levels give precision.

The trick is deciding how tall each node is. You do not compute it — you flip a coin. On insert, a node gets level 1, and while a coin keeps coming up heads (probability `p`, typically `1/4` or `1/2`), it grows one level taller. That is the whole balancing strategy. Pugh's paper shows the expected number of levels is `log_1/p(n)` and expected search cost is O(log n), *"without reference to the number of elements in the list"* — no global rebalance ever runs.

## Search and insert in ~30 lines

```python
import random

class Node:
    def __init__(self, key, level):
        self.key = key
        self.next = [None] * (level + 1)  # forward pointer per level

class SkipList:
    def __init__(self, max_level=16, p=0.5):
        self.max_level, self.p = max_level, p
        self.level = 0
        self.head = Node(None, max_level)

    def _random_level(self):
        lvl = 0
        while random.random() < self.p and lvl < self.max_level:
            lvl += 1
        return lvl

    def search(self, key):
        node = self.head
        for i in range(self.level, -1, -1):            # top lane down to level 0
            while node.next[i] and node.next[i].key < key:
                node = node.next[i]                     # walk right on this lane
        node = node.next[0]
        return node is not None and node.key == key

    def insert(self, key):
        update = [self.head] * (self.max_level + 1)     # predecessor at each level
        node = self.head
        for i in range(self.level, -1, -1):
            while node.next[i] and node.next[i].key < key:
                node = node.next[i]
            update[i] = node
        lvl = self._random_level()
        if lvl > self.level:                            # grow the list's height
            for i in range(self.level + 1, lvl + 1):
                update[i] = self.head
            self.level = lvl
        new = Node(key, lvl)
        for i in range(lvl + 1):                        # splice in at each level
            new.next[i] = update[i].next[i]
            update[i].next[i] = new
```

The `update` array is the key insight for insert: as you descend, you record the last node you touched on each level. Those are exactly the pointers that need to be re-linked to the new node. Deletion is the mirror image — find the same predecessors and unlink.

## Why production systems pick it

**Simplicity that survives concurrency.** There are no rotations that reshape a subtree, so an insert only rewrites a handful of `next` pointers near one location. That locality is what makes skip lists lock-friendly: Java's `ConcurrentSkipListMap` is lock-free via CAS on those pointers, and RocksDB notes its default skiplist memtable is *the only* memtable type that supports concurrent inserts.

**Ordered range scans come for free.** Because level 0 is a fully sorted linked list, "give me everything between X and Y" is a search plus a linear walk. That is why Redis backs sorted sets (ZSETs) with a skip list plus a hash table — the hash gives O(1) score lookup, the skip list gives O(log n) rank and range queries like `ZRANGEBYSCORE`. LSM-tree stores (RocksDB, LevelDB) use it as the in-memory memtable so flushed SSTables are already sorted.

The tradeoff is honesty about the word *expected*: a bad run of coin flips can make an operation O(n). In practice, with `p = 1/4` and a `MaxLevel` around `log_1/p(N)`, the variance is small enough that every major system above ships it as-is.

**Try next:** add a `delete(key)` method reusing the `update` array, then instrument `search` to count nodes visited and confirm it stays near `log2(n)` as you insert 1M keys.
