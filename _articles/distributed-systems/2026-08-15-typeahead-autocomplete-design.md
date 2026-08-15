---
title: "Typeahead Autocomplete: Precomputed Top-k per Prefix"
date: 2026-08-15
track: distributed-systems
summary: "Autocomplete has roughly a 100 ms budget per keystroke, so ranking at query time is excluded; the top-k completions are precomputed per prefix and served with one lookup. Covers the trie with cached top-k, the Redis sorted-set-per-prefix model (Prefixy), batch versus streaming aggregation, prefix sharding, and the personalization split in Facebook's typeahead."
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

**Gist.** Every keystroke in a search box issues a request whose answer must arrive before the next keystroke; Facebook's typeahead write-up frames the constraint as a budget on the order of 100 ms, past which a result is no longer useful because the query text has already changed. That budget excludes the direct design — scan the phrases matching the prefix, rank by frequency, at request time — so the **top-k completions are precomputed for every prefix** and a request becomes a single lookup. The cost is space and staleness: each phrase is replicated along its whole prefix path, and the precomputed ranking reflects whenever the aggregation last ran.

## Trie with top-k cached at every node

The classical structure is a trie over historical queries in which **each node additionally stores the k best completions in its subtree**. Lookup walks the prefix in O(len(prefix)) character steps and returns the node's cached list. **No subtree traversal happens at query time**; that is the invariant the structure exists to maintain, and every write must restore it.

Maintaining it means a phrase of length L is pushed into L bounded heaps, one per proper prefix. Hot phrases are therefore replicated along their entire prefix path — memory traded for read latency. Production variants compress paths (radix/Patricia), cap the depth at which full top-k lists are stored, and serialize the structure into **immutable snapshots swapped atomically** on the serving hosts. Elasticsearch's completion suggester is this idea productized: it uses purpose-built in-memory structures (finite state transducers, FSTs) that the reference documents as "costly to build," and builds them at index time rather than query time.

### Implementation sketch (Scala)

```scala
final case class Suggestion(phrase: String, count: Long)

final class Node(val children: scala.collection.mutable.Map[Char, Node] =
                   scala.collection.mutable.Map.empty):
  // Min-ordered by count: the head is the weakest survivor, evicted first.
  val topK: scala.collection.mutable.PriorityQueue[Suggestion] =
    scala.collection.mutable.PriorityQueue.empty(
      Ordering.by[Suggestion, Long](-_.count))

final class TopKTrie(k: Int):
  private val root = Node()

  /** Restores the invariant "every prefix node holds the k best of its
    * subtree" by touching exactly len(phrase) nodes. */
  def insert(phrase: String, count: Long): Unit =
    var node = root
    for ch <- phrase do
      node = node.children.getOrElseUpdate(ch, Node())
      node.topK.enqueue(Suggestion(phrase, count))
      if node.topK.size > k then node.topK.dequeue()

  def suggest(prefix: String): Seq[String] =
    prefix
      .foldLeft(Option(root))((n, ch) => n.flatMap(_.children.get(ch)))
      .toSeq
      .flatMap(_.topK.toSeq.sortBy(-_.count).map(_.phrase))
```

A batch build feeds each distinct phrase once with its aggregated count; re-inserting the same phrase with a new count admits duplicates into the heaps and must be deduplicated by the builder.

## The Redis alternative: one sorted set per prefix

A literal trie is not required. **Prefixy**, a published case study, and a line of Redis practice starting with antirez's post flatten the trie into a keyspace: for every prefix, keep a **Redis sorted set** mapping completion to score.

- Read: `ZREVRANGE "how " 0 4` returns the top five completions in O(log N + k) over one network round trip.
- Write: for each prefix of a submitted query `q`, issue `ZINCRBY prefix 1 q`. The score bump and the re-ranking are a **single atomic command per prefix**.

antirez's original construction differs: it stores *every prefix as a member* of one sorted set with score 0, so lexicographic ordering makes a `ZRANK` plus `ZRANGE` binary search yield completions. The set-per-prefix model carries real frequency scores instead, at the cost of one key per distinct prefix. Prefixy describes this as trading space for time: the prefix explosion is accepted because read latency is the product.

| | Trie + cached top-k | Redis ZSET per prefix | ES completion suggester |
|---|---|---|---|
| Read | O(len(prefix)), in-process | O(log N + k), 1 network hop | FST walk, HTTP hop |
| Update | rebuild or patch snapshot | `ZINCRBY`, live | reindex documents |
| Freshness | one batch cadence | one write round trip | near-real-time index |
| Ops | custom service plus snapshots | stock Redis, sharded by key | reuses an existing cluster |

## Obtaining the counts: batch versus streaming

Scores derive from a log of submitted queries, aggregated one of two ways.

- **Offline aggregation.** A MapReduce or Spark job aggregates the query log on a fixed cadence, computes per-phrase counts with time decay, builds top-k per prefix, ships immutable trie snapshots, and swaps pointers atomically. The pipeline is testable, and **rank stability is a property rather than a defect** — suggestions should not reorder between adjacent keystrokes.
- **Streaming updates.** The query stream is consumed and applied with `ZINCRBY` (the Prefixy model), or fed to a stream processor maintaining per-prefix top-k. This is what surfaces newly trending queries without waiting for the next batch. Two costs follow: **hot-key contention on short prefixes**, since every query increments its one- and two-character prefixes, and **unbounded phrase cardinality** per prefix, which requires trimming (`ZREMRANGEBYRANK` after insert) and, for the long tail of counts, a sketch such as the one described in the [heavy hitters article](/articles/distributed-systems/2026-08-10-count-min-sketch).

Hybrids are common: the batch job owns the baseline ranking and a smaller streaming layer overlays trending queries.

## Sharding, the client, and personalization

**Shard by prefix, not by phrase.** Hashing the first two or three characters places `"ho"` and its whole subtree on one shard, so any request touches exactly one shard. Alphabetical range sharding ('a–d', 'e–h', …) produces hot shards because letter frequency is skewed; hashing spreads them, and individually scorching prefixes can be split onto dedicated replicas. One-character prefixes are simultaneously the hottest and the least selective, which is why many systems do not query the backend below two or three characters and serve the shortest prefixes from a small static list held in the client or at the edge.

The **client is part of the system**. Keystrokes are debounced so that a fast typist does not emit one request per character; in-flight requests are cancelled when a newer one supersedes them; **responses must be matched to their prefix or a sequence number and discarded when out of order**, since a stale reply that arrives last will otherwise overwrite the correct suggestions; and a local prefix-to-results cache keeps a backspace off the network.

**Personalization** is the axis on which Facebook's typeahead differs from an unpersonalized search bar. On first focus, the browser bootstraps the user's first-degree graph — friends, pages, events — into a client-side cache, so the likeliest results complete without a network call. Server-side, a stateless **aggregator** fans each prefix out to specialized leaf services, including a global unpersonalized index and a graph-proximity service, then merges and re-ranks the results using the user's signals, with memcached in front of the global leaves. The structural pattern is to keep the shared prefix-to-top-k machinery unpersonalized and therefore cacheable, and to re-rank only the small final candidate set per user.

## Pitfalls

- **Re-inserting a phrase with an updated count into a live trie.** The old entry remains in every ancestor heap, so the same phrase appears twice in a suggestion list with different scores; batch builders must aggregate counts before insertion.
- **Alphabetical range sharding.** Letter frequency is skewed, so the shard owning common initial letters saturates while others idle.
- **Untrimmed sorted sets under streaming writes.** Every distinct query ever typed under a prefix stays resident, so memory grows with query cardinality rather than with k.
- **No sequence number on client responses.** A slower request for a shorter prefix returns after a faster one for a longer prefix, and the suggestion list reverts to results for text the user has already passed.
- **Querying the backend on the first character.** Single-character prefixes concentrate the highest request rate on the fewest distinct answers, and the answers are too unselective to be useful.
- **Treating batch freshness as a bug.** Rebuilding rankings continuously makes suggestions reorder between keystrokes, which is visible to the user as flicker in the list.
