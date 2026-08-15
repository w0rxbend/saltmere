---
title: 'The Inverted Index and Full-Text Search: Lucene and Elasticsearch Mechanics'
date: 2026-08-10
track: distributed-systems
summary: 'Full-text search rests on one data structure: the inverted index, mapping each term to a postings list of document ids. This article walks the index and its positions for phrase queries, the analysis pipeline that must be applied identically at index and query time, relevance ranking from TF-IDF to BM25 (the Lucene and Elasticsearch default since Lucene 6.0) with its k1 saturation and b length-normalization parameters, and Lucene immutable segments that merge like an LSM-tree.'
reading_time: 7
tags:
- inverted-index
- full-text-search
- bm25
- lucene
- elasticsearch
- search
sources:
- title: 'Practical BM25 - Part 2: The BM25 Algorithm and its Variables (Elastic Blog)'
  url: https://www.elastic.co/blog/practical-bm25-part-2-the-bm25-algorithm-and-its-variables
- title: 'Practical BM25 - Part 3: Considerations for Picking b and k1 in Elasticsearch (Elastic Blog)'
  url: https://www.elastic.co/blog/practical-bm25-part-3-considerations-for-picking-b-and-k1-in-elasticsearch
- title: 'Robertson & Zaragoza — The Probabilistic Relevance Framework: BM25 and Beyond'
  url: https://www.staff.city.ac.uk/~sbrp622/papers/foundations_bm25_review.pdf
- title: 'LUCENE-6789: change IndexSearcher default similarity to BM25 (ASF Jira)'
  url: https://issues.apache.org/jira/browse/LUCENE-6789
- title: BM25Similarity (Apache Lucene core API)
  url: https://lucene.apache.org/core/8_2_0/core/org/apache/lucene/search/similarities/BM25Similarity.html
- title: 'Robertson & Zaragoza, The Probabilistic Relevance Framework: BM25 and Beyond (FnTIR, 2009)'
  url: https://dl.acm.org/doi/abs/10.1561/1500000019
- title: Elastic blog — Elasticsearch from the Top Down (segments, distributed search)
  url: https://www.elastic.co/blog/found-elasticsearch-top-down
- title: Apache Lucene — Core News (release history)
  url: https://lucene.apache.org/core/corenews.html
- title: quickwit-oss/tantivy — full-text search engine library in Rust
  url: https://github.com/quickwit-oss/tantivy
---

**Gist.** Substring matching over raw text (`WHERE body LIKE '%quick brown%'`) scans every row and cannot rank the results, costing O(rows x document length) per query. Full-text engines invert the mapping — term to sorted **postings list** of document ids — so a query touches only documents containing the query terms, and scores them with BM25. The cost is a second copy of the corpus in normalized form, an analysis pipeline that must be applied identically at write and read time, and an index that is built from immutable segments merged in the background rather than updated in place.

## The inverted index

A forward index maps document to terms ("which words occur in document 7?"). Retrieval needs the opposite direction: term to the documents containing it. That is the *inverted* index — a dictionary of terms, each pointing at a postings list of document ids:

```
brown  -> [1, 2, 3]
quick  -> [1, 2]
fox    -> [1]
```

Answering `quick AND brown` intersects two sorted postings lists: a linear merge over the documents containing those terms, never reading the rest of the corpus. **Postings lists are stored sorted by document id so that intersection is a single simultaneous walk over both lists**, and they are compressed (delta-encoded gaps plus variable-byte or PForDelta coding) so a term occurring in millions of documents remains cheap to read.

Boolean matching covers only set membership. **Phrase queries** — `"quick brown"` as an adjacent pair rather than two words scattered through the document — require that each posting also record the **positions** at which the term occurs in that document:

```
quick -> {1:[0], 2:[0,3]}
brown -> {1:[1], 2:[1]}
```

Document 1 has `quick` at position 0 and `brown` at position 1; they are adjacent, so the phrase matches. **The phrase algorithm intersects the document sets first, then verifies that for some occurrence of the first term at position `p`, term `i` of the phrase occurs at `p + i`.** Lucene uses the same positional data for highlighting and for proximity (slop) queries.

## The analysis pipeline and its symmetry invariant

Raw text is not searchable as stored. Before reaching the index it passes through an **analyzer**: a tokenizer followed by a chain of token filters. A representative English pipeline:

1. **Tokenize** — split on non-word boundaries into terms.
2. **Lowercase** — so that `Quick` and `quick` collapse to one term.
3. **Stop words** — optionally drop very common tokens (`the`, `a`, `of`) that carry little discriminating signal.
4. **Stemming or lemmatization** — reduce `foxes`, `jumping`, `ran` toward a root. Stemming is suffix truncation (`foxes` to `fox`); lemmatization is dictionary-driven and yields real lemmas (`ran` to `run`). Elasticsearch ships algorithmic stemmer filters (`porter_stem`, `kstem`) and a dictionary-driven `hunspell` filter.

The invariant: **the same analyzer must run at index time and at query time.** If documents are stemmed to `fox` while the query is matched against the literal token `foxes`, the query term is absent from the dictionary and the result set is empty. Index and query must meet in the same normalized term space. Elasticsearch permits a distinct `search_analyzer` — for example to expand synonyms on one side only — but the default configuration is symmetric.

## Ranking: TF-IDF, then BM25

Matching produces a *set*; a search result is a *ranked list*. The classical scoring intuition is **term frequency-inverse document frequency (TF-IDF)**: a term counts for more in a document the more often it occurs there (term frequency, TF), and counts for more overall the rarer it is in the corpus (inverse document frequency, IDF — `brown` occurring in every document discriminates nothing, while a term occurring in three documents discriminates strongly).

**BM25** ("Best Matching 25", from the Okapi work of Robertson and colleagues, reviewed in Robertson and Zaragoza's *Probabilistic Relevance Framework*) addresses two weaknesses of plain TF-IDF. Under **[LUCENE-6789](https://issues.apache.org/jira/browse/LUCENE-6789), shipped in Lucene 6.0 (April 2016), BM25 is the default similarity in Lucene**, and therefore in Elasticsearch and Solr. Summing over query terms `t`:

```
score(D, Q) = Σ  IDF(t) * [ f(t,D) * (k1 + 1) ] / [ f(t,D) + k1 * (1 - b + b * |D|/avgdl) ]
```

where `f(t,D)` is the frequency of the term in `D`, `|D|` the document length in terms, `avgdl` the mean document length, and `IDF(t) = ln(1 + (N - n(t) + 0.5) / (n(t) + 0.5))` over a corpus of `N` documents of which `n(t)` contain the term. The two parameters carry the behaviour:

- **`k1` — term-frequency saturation** (Lucene default **1.2**). Under raw TF a document mentioning `quick` twenty times scores twenty times one mention. BM25 passes TF through a saturating function: the `f/(f + k1 * ...)` form rises steeply over the first few occurrences and then flattens toward an asymptote. **`k1` controls the rate of saturation**: a lower `k1` saturates sooner, so the third occurrence contributes little; a higher `k1` continues to reward repetition.
- **`b` — length normalization** (Lucene default **0.75**). A 5,000-word document contains `quick` more often than a short one without being more about it. The factor `(1 - b + b * |D|/avgdl)` discounts documents longer than `avgdl`. **`b = 0` disables normalization; `b = 1` applies it in full.** Elastic's *Practical BM25* series discusses selecting these per field, noting that short `title` fields and long `body` fields do not necessarily want the same `b`.

### Implementation sketch (Scala)

Analysis, a positional inverted index, BM25 scoring, and a phrase query over positions. The same `analyze` function serves documents and queries, which is the symmetry invariant made structural.

```scala
val stop = Set("the", "a", "is", "of", "to", "and", "in")

def analyze(text: String): Vector[String] =
  "[a-z0-9]+".r.findAllIn(text.toLowerCase).toVector.filterNot(stop)

val docs = Map(
  1 -> "The quick brown fox jumps",
  2 -> "Quick brown foxes are quick and clever",
  3 -> "A lazy brown dog sleeps in the sun")

val analyzed: Map[Int, Vector[String]] = docs.view.mapValues(analyze).toMap
val length: Map[Int, Int] = analyzed.view.mapValues(_.size).toMap
val n = docs.size
val avgdl = length.values.sum.toDouble / n

// term -> docId -> ascending positions
val index: Map[String, Map[Int, Vector[Int]]] =
  analyzed.toVector
    .flatMap((did, terms) => terms.zipWithIndex.map((t, p) => (t, did, p)))
    .groupMap(_._1)(t => (t._2, t._3))
    .view.mapValues(_.groupMap(_._1)(_._2).view.mapValues(_.sorted).toMap)
    .toMap

def bm25(query: String, k1: Double = 1.2, b: Double = 0.75): Seq[(Int, Double)] =
  analyze(query).foldLeft(Map.empty[Int, Double].withDefaultValue(0.0)) { (acc, t) =>
    val postings = index.getOrElse(t, Map.empty)
    val df = postings.size
    val idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
    postings.foldLeft(acc) { case (m, (did, positions)) =>
      val f = positions.size.toDouble
      val norm = f + k1 * (1 - b + b * length(did) / avgdl)
      m.updated(did, m(did) + idf * f * (k1 + 1) / norm)
    }
  }.toSeq.sortBy(-_._2)

def phrase(query: String): Set[Int] =
  val terms = analyze(query)
  val candidates = terms.map(t => index.getOrElse(t, Map.empty).keySet).reduce(_ intersect _)
  candidates.filter: did =>
    index(terms.head)(did).exists: p =>
      terms.indices.forall(i => index(terms(i))(did).contains(p + i))
```

Document 2 outranks document 1 for `quick brown` because it contains `quick` twice, but saturation means the second occurrence contributes less than the first, and the greater length is discounted through `b`, so the lead is bounded. The phrase query rejects document 3 — which contains `brown` with no adjacent `quick` — from positions alone, whereas a `LIKE` scan could establish the same only by re-reading each document's full text.

## Immutable segments that merge: the LSM analogy

Index construction at scale mirrors log-structured storage engines. **Lucene never mutates an index in place.** A batch of documents is analyzed in memory and flushed to a **segment**: a small, self-contained, immutable inverted index on disk. New writes create new segments; a query fans out over all current segments and merges the per-segment results. **Deletions are recorded as tombstone bits, so the term data for a deleted document remains in the segment until it is removed by a merge.**

Unchecked, the segment count grows and every query touches many files, so a background **merge** policy combines small segments into fewer larger ones and physically drops tombstoned documents in the process. Buffer in memory, flush immutable sorted runs, merge in the background, trading write amplification for read performance: that is the **[LSM-tree](/articles/distributed-systems/2026-08-10-lsm-trees-vs-b-trees)** shape, with segments in the role of SSTables and merge in the role of compaction.

## Distributed search: shard by document, scatter-gather

Elasticsearch and OpenSearch partition an index **by document**: each document is routed to one shard — itself a complete Lucene index — by a hash of its identifier. A query cannot be routed the same way, since the relevant documents may live on any shard, so it fans out:

1. **Query phase (scatter):** the coordinating node sends the query to one copy of every shard; each returns its local top-k as `(doc_id, score)` pairs, without document bodies.
2. **Merge:** the coordinator heap-merges the N shards x k entries into a global top-k.
3. **Fetch phase (gather):** it retrieves `_source` for the surviving documents only, from the shards that own them.

## Pitfalls

- **Asymmetric analysis returns zero hits with no error.** Documents stemmed to `fox` while queries are matched on the unstemmed token `foxes` never intersect; the query is well formed and the result set is empty.
- **Deep pagination costs grow with the offset, not the page size.** A request with `from=10000` forces every shard to return 10,010 candidates to the coordinator for merging; `search_after` avoids the growing per-shard prefix.
- **BM25 statistics are per shard.** Each shard computes IDF from its own document counts, so identical documents on differently populated shards can receive different scores. The divergence is most visible on small indices; as shards grow, their term statistics converge toward the corpus-wide values.
- **`b = 0` removes length normalization entirely.** Long documents then accumulate term frequency without penalty and dominate the ranking for common terms.
- **A postings list stored without positions cannot answer phrase or proximity queries.** The failure is at index build time, not query time: the positional data must be written when the document is indexed.
- **Deleted documents continue to occupy the index and influence file count until a merge runs.** Tombstones exclude them from results but not from the on-disk segment.
