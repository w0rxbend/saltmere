---
title: "Design Typeahead Autocomplete: Precompute Top-k, Then Argue About Where"
date: 2026-08-15
track: distributed-systems
summary: "Autocomplete has a ~100ms budget per keystroke, so nobody ranks at query time — you precompute top-k completions per prefix and argue about where they live: a trie with cached top-k per node, or a Redis sorted set per prefix (the Prefixy approach). Covers offline vs streaming aggregation, prefix sharding, and what Facebook's typeahead adds for personalization."
reading_time: 6
tags: [system-design, autocomplete, trie, redis, interview-prep]
sources:
  - title: "Keith Adams (Facebook Engineering) — The Life of a Typeahead Query (2010)"
    url: "https://engineering.fb.com/2010/05/17/web/the-life-of-a-typeahead-query/"
  - title: "antirez — Auto Complete with Redis"
    url: "https://oldblog.antirez.com/post/autocomplete-with-redis.html"
  - title: "Prefixy — Building a Scalable Prefix Search Service (case study)"
    url: "https://prefixy.github.io/"
  - title: "Elasticsearch Reference — Search Suggesters (completion suggester)"
    url: "https://www.elastic.co/docs/reference/elasticsearch/rest-apis/search-suggesters"
---

Every keystroke in a search box fires a query, and the answer must come back before the next keystroke — Facebook's typeahead team put the budget bluntly: with a ~100ms window, "late answers are wrong answers." That budget bans the obvious design (scan queries matching prefix, rank by frequency, at request time). The entire game is **precomputing the top-k completions for every prefix** and serving them with one cheap lookup. The interview is about where that precomputed data lives and how it gets refreshed.

## Trie with top-k cached at every node

The textbook structure: a trie over historical queries, where each node additionally stores the k best completions in its subtree. Lookup = walk the prefix (O(len(prefix))), return the node's cached list. No subtree traversal at query time — that's the point.

```python
import heapq

class Node:
    __slots__ = ("children", "topk")
    def __init__(self):
        self.children, self.topk = {}, []   # topk: [(count, phrase)]

class Trie:
    def __init__(self, k=5):
        self.root, self.k = Node(), k

    def insert(self, phrase, count):
        node = self.root
        for ch in phrase:
            node = node.children.setdefault(ch, Node())
            heapq.heappush(node.topk, (count, phrase))   # push onto every
            if len(node.topk) > self.k:                  # prefix's heap,
                heapq.heappop(node.topk)                 # evict the min
        # (a real build dedupes phrase re-inserts; fine for batch rebuild)

    def suggest(self, prefix):
        node = self.root
        for ch in prefix:
            if ch not in node.children:
                return []
            node = node.children[ch]
        return [p for _, p in sorted(node.topk, reverse=True)]
```

Space is the catch: a phrase of length L is pushed into L heaps, so hot phrases are replicated along their whole prefix path — deliberately trading memory for read speed. Production versions compress (radix/Patricia paths), cap depth (nobody needs suggestions for 60-char prefixes; store full top-k only for prefixes up to ~10 chars), and serialize the trie into weekly-built immutable snapshots. Elasticsearch's completion suggester is this idea productized — purpose-built in-memory structures (FSTs) that are "costly to build," which is why they're constructed at index time, not query time.

## The Redis alternative: a sorted set per prefix

You don't need a literal trie. **Prefixy** (a nice public case study) and a long line of Redis folklore starting with antirez's 2010 post flatten the trie into a hash: for every prefix, keep a **Redis sorted set** `prefix → {completion: score}`.

- Read: `ZREVRANGE "how " 0 4` → top-5 completions, O(log N + k), one round trip.
- Write: user submits query q → for each prefix of q: `ZINCRBY prefix 1 q`. Score bumps and re-ranking happen atomically in one command per prefix.

antirez's original trick predates `ZADD`-per-prefix designs: he stored *every prefix as a member* of one sorted set with score 0, so lexicographic ordering made a binary search (`ZRANK` + `ZRANGE`) return completions — clever when memory was tight, but the set-per-prefix model with real frequency scores is what you'd ship today. Prefixy's stated trade-off applies to both: "we trade space for time," accepting the prefix explosion because read speed is the product.

| | Trie + cached top-k | Redis ZSET per prefix | ES completion suggester |
|---|---|---|---|
| Read | O(len(prefix)), in-process | O(log N + k), 1 network hop | FST walk, HTTP hop |
| Update | rebuild/patch snapshot | `ZINCRBY`, live | reindex docs |
| Freshness | batch (hours–week) | seconds | near-real-time index |
| Ops | custom service + snapshots | stock Redis, easy sharding | you already run ES? |

## Getting the counts: weekly batch vs streaming

Where do scores come from? Log every submitted query, then:

- **Offline aggregation (the classic answer):** MapReduce/Spark job aggregates the query log (say, weekly or daily), computes per-phrase counts with time decay, builds top-k per prefix, ships immutable trie snapshots to servers, atomically swaps pointers. Simple, testable, and rank stability is a *feature* — suggestions shouldn't jitter per keystroke.
- **Streaming updates:** consume the query stream and `ZINCRBY` live (the Prefixy model), or push through a stream processor that maintains per-prefix top-k. Needed if "breaking news" must surface in minutes. The costs: hot-key contention on short prefixes, and unbounded phrase cardinality — cap ZSET sizes (`ZREMRANGEBYRANK` after insert) and consider a Count-Min Sketch for the long tail of counts, which the corpus covers in the [heavy hitters article](/articles/distributed-systems/2026-08-10-count-min-sketch).

Most real systems are hybrid: batch job owns the baseline ranking; a small streaming layer overlays trending queries.

## Sharding, the client, and personalization

**Shard by prefix**, not by phrase: hash the first 2–3 characters so `"ho"` and everything under it lands on one shard, and any request touches exactly one shard. Naive alphabetical range sharding ('a–d', 'e–h', ...) creates hot shards — letter frequency is wildly skewed — so hash prefixes, and further split individual scorching prefixes ("t", "th") onto dedicated replicas. Short prefixes (1 char) are both hottest and least useful; many systems don't even query the backend until 2–3 chars and serve single-char prefixes from a tiny static list cached at the edge or in the client.

The **client** is part of the system: debounce keystrokes (~50–150ms) so fast typers don't fire a request per character, cancel in-flight requests when a newer one supersedes them, discard out-of-order responses (attach the prefix or a sequence number), and cache prefix→results locally — a backspace should never hit the network.

**Personalization** is what separates Google-bar autocomplete from Facebook's typeahead. Facebook's architecture: on first focus, the browser *bootstraps* the user's first-degree graph (friends, pages, events) into client-side cache, so the likeliest results complete with zero network. Backend-side, a stateless **aggregator** fans each prefix out to specialized leaf services — a global (unpersonalized) index and a graph-proximity service — then merges and re-ranks with the user's signals, with memcached in front of the global leaves. The general pattern: keep the shared prefix→top-k machinery unpersonalized and cacheable, then re-rank the final ~20 candidates per user at the edge.

**Try next:** build the ZSET-per-prefix design against a real query log (AOL or Wikipedia clickstream), then add a 100ms-debounced HTML input and measure p99 keystroke-to-render — then try shipping the trie version and see which one you'd rather operate.
