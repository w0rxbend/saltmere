---
title: "The Inverted Index and Full-Text Search: How Lucene and Elasticsearch Actually Work"
date: 2026-08-10
track: distributed-systems
summary: "\"Design search\" is a system-design staple, and the whole thing rests on one data structure: the inverted index that maps each term to a postings list of doc ids. This walks the index and its positions for phrase queries, the analysis pipeline you must apply identically at index and query time, relevance ranking from TF-IDF to BM25 (the Lucene/Elasticsearch default since 2016) with the k1 saturation and b length-normalization knobs, and how Lucene builds immutable segments that merge like an LSM-tree. Includes a ~30-line Python inverted index plus BM25 scorer and a positional phrase query."
reading_time: 6
tags:
  - inverted-index
  - full-text-search
  - bm25
  - lucene
  - elasticsearch
sources:
  - title: "Practical BM25 - Part 2: The BM25 Algorithm and its Variables (Elastic Blog)"
    url: "https://www.elastic.co/blog/practical-bm25-part-2-the-bm25-algorithm-and-its-variables"
  - title: "Practical BM25 - Part 3: Considerations for Picking b and k1 in Elasticsearch (Elastic Blog)"
    url: "https://www.elastic.co/blog/practical-bm25-part-3-considerations-for-picking-b-and-k1-in-elasticsearch"
  - title: "Robertson & Zaragoza — The Probabilistic Relevance Framework: BM25 and Beyond"
    url: "https://www.staff.city.ac.uk/~sbrp622/papers/foundations_bm25_review.pdf"
  - title: "LUCENE-6789: change IndexSearcher default similarity to BM25 (ASF Jira)"
    url: "https://issues.apache.org/jira/browse/LUCENE-6789"
  - title: "BM25Similarity (Apache Lucene core API)"
    url: "https://lucene.apache.org/core/8_2_0/core/org/apache/lucene/search/similarities/BM25Similarity.html"
---

Ask a candidate to "design a search feature" and the weak answer reaches for `WHERE body LIKE '%quick brown%'`. That scan reads every row, matches raw bytes, can't rank results, and can't use an index — it's O(rows × doc length) on every query. The strong answer names the data structure the whole field is built on: the **inverted index**. Lucene — the library under Elasticsearch, OpenSearch, and Solr — is one very good implementation of it. This is what's happening under the hood.

## The inverted index

A forward index maps document → terms ("what words are in doc 7?"). Search needs the opposite: term → the documents that contain it. That's the *inverted* index, and it's just a dictionary of terms, each pointing at a **postings list** of doc ids:

```
brown  -> [1, 2, 3]
quick  -> [1, 2]
fox    -> [1]
```

To answer `quick AND brown`, you intersect two sorted postings lists — a linear merge over just the docs that contain those terms, never touching the rest of the corpus. Postings lists are kept sorted by doc id precisely so intersection is a cheap zipper walk, and they're compressed (delta-encoded gaps + variable-byte or PForDelta) so a term appearing in millions of docs still costs little to read.

Boolean matching is only half of it. To support **phrase queries** — `"quick brown"` as an adjacent pair, not two words scattered across the doc — each posting also stores the **positions** where the term occurs in that document:

```
quick -> {1:[0], 2:[0,3]}
brown -> {1:[1], 2:[1]}
```

Doc 1 has `quick` at position 0 and `brown` at 1 — adjacent, so it matches the phrase. A phrase query intersects the docs, then checks that for some occurrence of the first term at position `p`, every later term appears at `p+i`. (Lucene also stores this data to power highlighting and proximity/slop queries.)

## The analysis pipeline — and why it must be symmetric

Raw text isn't searchable as-is. Before anything hits the index, it runs through an **analyzer**: a tokenizer plus a chain of token filters. A typical English pipeline:

1. **Tokenize** — split on non-word boundaries into terms.
2. **Lowercase** — so `Quick` and `quick` collide.
3. **Stop words** — optionally drop ultra-common tokens (`the`, `a`, `of`) that carry little signal.
4. **Stemming / lemmatization** — reduce `foxes`, `jumping`, `ran` toward a root. Stemming is crude suffix-chopping (`foxes` → `fox`); lemmatization is dictionary-based and returns real lemmas (`ran` → `run`). Elasticsearch ships both (`porter_stem`, `kstem`, dictionary lemmatizers).

The rule that trips people up in interviews: **the exact same analyzer must run at index time and at query time.** If you stem documents to `fox` but search the literal token `foxes`, the query term never matches the indexed term and you get zero hits. The index and the query have to meet in the same normalized term space. (Elasticsearch lets you set a different `search_analyzer`, but only deliberately — e.g. to skip synonym expansion on one side. The default is symmetry.)

## Ranking: TF-IDF, then BM25

Matching gives you a *set*; users want a *ranked list*. The classic scoring intuition is **TF-IDF**: a term matters more in a document the more often it appears there (term frequency, TF), and matters more overall the rarer it is across the corpus (inverse document frequency, IDF — `brown` in every doc is uninformative; `defenestration` in three docs is gold).

Plain TF-IDF has two weaknesses, and **BM25** ("Best Matching 25," from Robertson and Sparck Jones's Okapi work) fixes both. Since **[LUCENE-6789](https://issues.apache.org/jira/browse/LUCENE-6789), shipped in Lucene 6.0 (April 2016), BM25 is the default similarity in Lucene** — and therefore in Elasticsearch and Solr. The formula, summing over query terms `t`:

```
score(D, Q) = Σ  IDF(t) · [ f(t,D) · (k1 + 1) ] / [ f(t,D) + k1 · (1 - b + b · |D|/avgdl) ]
```

where `f(t,D)` is the term's frequency in `D`, `|D|` is the document length in terms, `avgdl` the average document length, and `IDF(t) = ln(1 + (N − n(t) + 0.5) / (n(t) + 0.5))` for a corpus of `N` docs with `n(t)` containing the term. The two knobs are the whole point:

- **`k1` — term-frequency saturation** (default **1.2**). In raw TF, a document mentioning `quick` twenty times scores 20× one mention — spammy and wrong. BM25 feeds TF through a saturating curve: the `f/(f + k1·…)` shape rises fast for the first few occurrences then flattens toward an asymptote. `k1` sets *how fast* it saturates. Lower `k1` saturates sooner (the 3rd occurrence barely helps); higher `k1` keeps rewarding repetition longer. This is the single biggest conceptual upgrade over TF-IDF.
- **`b` — length normalization** (default **0.75**). A 5,000-word document naturally contains `quick` more often than a tweet, without being more *about* it. The `(1 − b + b·|D|/avgdl)` factor discounts long documents relative to `avgdl`. `b = 0` disables normalization entirely; `b = 1` applies it fully; 0.75 is the tuned middle. Elastic's *Practical BM25* series walks through picking these per-field — short `title` fields often want a different `b` than long `body` fields.

## A tiny index + BM25 scorer

Thirty-odd lines: analyze, build the positional inverted index, score with BM25, and answer a phrase query from positions.

```python
import re, math
from collections import defaultdict

def analyze(text):                      # SAME pipeline for docs and queries
    STOP = {"the","a","is","of","to","and","in"}
    toks = re.findall(r"[a-z0-9]+", text.lower())
    return [t for t in toks if t not in STOP]

docs = {1: "The quick brown fox jumps",
        2: "Quick brown foxes are quick and clever",
        3: "A lazy brown dog sleeps in the sun"}

index = defaultdict(lambda: defaultdict(list))   # term -> {doc_id: [positions]}
length, N = {}, len(docs)
for did, raw in docs.items():
    terms = analyze(raw)
    length[did] = len(terms)
    for pos, t in enumerate(terms):
        index[t][did].append(pos)
avgdl = sum(length.values()) / N

def bm25(query, k1=1.2, b=0.75):
    scores = defaultdict(float)
    for t in analyze(query):
        postings = index.get(t, {})
        n = len(postings)                                    # docs containing t
        idf = math.log(1 + (N - n + 0.5) / (n + 0.5))
        for did, positions in postings.items():
            f = len(positions)                               # term freq in doc
            norm = f + k1 * (1 - b + b * length[did] / avgdl)
            scores[did] += idf * (f * (k1 + 1)) / norm
    return sorted(scores.items(), key=lambda x: -x[1])

def phrase(query):                                           # exact adjacency
    terms = analyze(query)
    for did in set.intersection(*[set(index.get(t, {})) for t in terms]):
        if any(all((p + i) in index[terms[i]][did] for i in range(len(terms)))
               for p in index[terms[0]][did]):
            yield did

print(bm25("quick brown"))          # [(2, 0.735), (1, 0.657), (3, 0.134)]
print(list(phrase("quick brown")))  # [1, 2]
print(list(phrase("brown fox")))    # [1]
```

Doc 2 wins `quick brown` because it contains `quick` twice — but note BM25's saturation means the second occurrence adds *less* than the first, and its longer length is discounted by `b`, so it doesn't run away with the score. The phrase query rejects doc 3 (it has `brown` but no adjacent `quick`) using positions alone — something a `LIKE` scan could only do by re-reading every document's full text.

## Immutable segments that merge — the LSM analogy

The last piece is *how the index is built at scale*, and it mirrors storage engines exactly. Lucene never mutates the index in place. A batch of documents is analyzed in memory and flushed to a **segment**: a small, self-contained, **immutable** inverted index on disk. New writes go to new segments; a query fans out across *all* current segments and merges their results. Deletes are just tombstone bits — the term data lingers until cleanup.

Left alone, segment count would explode and every query would touch hundreds of files, so a background **merge** policy periodically combines small segments into fewer larger ones, physically dropping tombstoned docs along the way. Buffer in memory → flush immutable sorted runs → merge in the background, trading write amplification for fast reads: that is precisely the **[LSM-tree](/articles/distributed-systems/2026-08-10-lsm-trees-vs-b-trees)** shape, with segments playing the role of SSTables and merge playing compaction. Recognizing that a search index and a RocksDB store are the same idea in different clothes is exactly the kind of connection interviewers are listening for.

**Try next:** run the code above, then bump `k1` from 1.2 to 3.0 and re-score `quick brown` — watch doc 2's lead over doc 1 widen as repeated `quick` gets rewarded more; then set `b = 0` and see the length penalty on the longer doc 2 disappear.
