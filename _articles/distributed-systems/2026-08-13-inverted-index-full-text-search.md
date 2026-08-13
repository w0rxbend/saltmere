---
title: "Inverted indexes and BM25: how full-text search actually works"
date: 2026-08-13
track: distributed-systems
summary: "Full-text search is three ideas stacked: an inverted index mapping terms to postings lists, BM25 scoring with saturating term frequency and length normalization, and immutable segments merged in the background. Plus the distributed part — shard by document, scatter-gather the query — and a 30-line Python index you can run."
reading_time: 5
tags: [inverted-index, bm25, lucene, elasticsearch, search]
sources:
  - title: "Robertson & Zaragoza, The Probabilistic Relevance Framework: BM25 and Beyond (FnTIR, 2009)"
    url: "https://dl.acm.org/doi/abs/10.1561/1500000019"
  - title: "Elastic blog — Practical BM25, Part 2: The BM25 Algorithm and its Variables"
    url: "https://www.elastic.co/blog/practical-bm25-part-2-the-bm25-algorithm-and-its-variables"
  - title: "Elastic blog — Elasticsearch from the Top Down (segments, distributed search)"
    url: "https://www.elastic.co/blog/found-elasticsearch-top-down"
  - title: "Apache Lucene — Core News (release history)"
    url: "https://lucene.apache.org/core/corenews.html"
  - title: "quickwit-oss/tantivy — full-text search engine library in Rust"
    url: "https://github.com/quickwit-oss/tantivy"
---

A B-tree answers "give me the row where id = 42." Search answers the transposed question: "give me the documents containing *term* X, best first." The data structure for that is the inverted index, and everything Lucene, Elasticsearch, OpenSearch, and Tantivy do is elaboration on it.

## Building the index: analysis, then postings

Indexing runs each document through an **analysis chain**: tokenize ("The Quick Foxes" → `the`, `quick`, `foxes`), lowercase, drop stopwords, stem (`foxes` → `fox`). The same chain must run on queries — most "search returns nothing" bugs are analyzer mismatches between index time and query time.

The output is inverted: instead of doc → terms, you store term → **postings list** — the sorted list of document IDs containing the term, typically with per-doc term frequency and token positions (positions are what make phrase queries possible):

```
"fox"   -> [ (doc1, tf=2, pos=[4,17]), (doc7, tf=1, pos=[3]) ]
"quick" -> [ (doc1, tf=1, pos=[3]),    (doc4, tf=1, pos=[9]) ]
```

A term dictionary (FST in Lucene) maps each term to its postings; postings are delta-encoded and block-compressed since sorted doc IDs compress extremely well. Query `quick AND fox` = intersect two sorted lists; `OR` = merge. Ranking is where BM25 comes in.

## Scoring: BM25

For query Q and document D, sum over query terms:

```
score(D,Q) = Σ  IDF(q) · f(q,D) · (k1 + 1)
   q∈Q         ───────────────────────────────────
               f(q,D) + k1 · (1 − b + b · |D|/avgdl)

IDF(q) = ln(1 + (N − n(q) + 0.5) / (n(q) + 0.5))
```

Three intuitions, one per variable group:

- **IDF**: rare terms carry more signal. A term in 3 of 1M docs scores high; "the" scores near zero.
- **k1 (default 1.2)**: term frequency **saturates**. The 2nd occurrence of "fox" adds a lot, the 20th adds almost nothing — this is BM25's big improvement over raw TF-IDF, where TF grows unboundedly.
- **b (default 0.75)**: **length normalization**. A 10-term match in a tweet means more than in a novel; `b` controls how much the doc's length relative to the average (`avgdl`) discounts TF. `b=0` disables it.

A complete index with BM25 in ~30 lines:

```python
import math, re
from collections import Counter, defaultdict

class Index:
    def __init__(self):
        self.postings = defaultdict(dict)   # term -> {doc_id: tf}
        self.doc_len, self.k1, self.b = {}, 1.2, 0.75

    def add(self, doc_id, text):
        terms = re.findall(r"[a-z0-9]+", text.lower())
        self.doc_len[doc_id] = len(terms)
        for t, tf in Counter(terms).items():
            self.postings[t][doc_id] = tf

    def search(self, query, k=5):
        N, avgdl = len(self.doc_len), sum(self.doc_len.values()) / len(self.doc_len)
        scores = defaultdict(float)
        for t in re.findall(r"[a-z0-9]+", query.lower()):
            plist = self.postings.get(t, {})
            idf = math.log(1 + (N - len(plist) + 0.5) / (len(plist) + 0.5))
            for d, tf in plist.items():
                norm = self.k1 * (1 - self.b + self.b * self.doc_len[d] / avgdl)
                scores[d] += idf * tf * (self.k1 + 1) / (tf + norm)
        return sorted(scores.items(), key=lambda x: -x[1])[:k]

ix = Index()
ix.add(1, "the quick brown fox jumps over the lazy dog")
ix.add(2, "quick quick quick fox")
ix.add(3, "a slow brown dog")
print(ix.search("quick fox"))   # doc 2 first: high tf, short doc
```

## Why segments are immutable

Postings lists are heavily compressed and tightly interlinked — updating one in place is impractical. Lucene therefore writes **immutable segments**: buffered docs are flushed as a new mini-index; a search runs over all live segments and merges results. Deletes are just tombstone bits ("live docs" bitmap); updates are delete + reindex. Background **merges** rewrite several small segments into one large one, dropping tombstoned docs for real — the same compaction economics as LSM-trees, and for the same reason: sequential immutable writes beat in-place mutation. Immutability also buys free concurrency (readers need no locks) and OS page-cache friendliness. The visible cost is **near-real-time** semantics: a doc isn't searchable until the next refresh/flush (Elasticsearch defaults to 1 s).

## Distributed search: shard by document, scatter-gather

Elasticsearch/OpenSearch partition an index **by document**: each doc is routed to one shard (a full Lucene index) by hash of its ID — consistent with the partitioning schemes covered elsewhere in this track. A query can't be routed, so it fans out:

1. **Query phase (scatter):** the coordinating node sends the query to one copy of every shard; each returns its local top-k as `(doc_id, score)` — no documents yet.
2. **Merge:** the coordinator heap-merges N shards × k entries into a global top-k.
3. **Fetch phase (gather):** it fetches the actual `_source` for only those winners from the shards that own them.

Two classic gotchas: deep pagination (`from=10000` forces every shard to return 10 010 candidates — hence `search_after`), and per-shard IDF — each shard computes BM25 from its own statistics, so scores can differ for identical docs on differently-populated shards. With realistic shard sizes term statistics even out; Elastic's Practical BM25 series shows the small-index case where they don't.

## Modern context

Lucene remains the engine under Elasticsearch, OpenSearch, and Solr; the 10.x line (Lucene 10.0 shipped October 2024, latest 10.5.1 as of August 2026) focused on hardware efficiency — better I/O parallelism and SIMD-friendly execution. Tantivy is the same architecture in Rust (segments, FST dictionaries, BM25) and powers Quickwit. The postings + BM25 core has also become the "sparse" half of hybrid search, fused with vector retrieval — but that's another article.

**Try next:** extend the Python index with positions and implement a two-word phrase query, then check your BM25 scores against Elasticsearch's `_explain` API for the same three documents.
