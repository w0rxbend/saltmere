---
title: "Vector search at scale: HNSW and the approximate nearest-neighbor toolbox"
date: 2026-08-13
track: distributed-systems
summary: "Exact k-nearest-neighbor search over 100M embeddings is a full scan per query, so every production vector store approximates. HNSW's skip-list-of-graphs, the M and ef parameters, IVF and product quantization in a paragraph each, and the filtering problem — with a pgvector 0.8.2 example."
reading_time: 7
tags: [vector-search, hnsw, ann, pgvector, embeddings]
sources:
  - title: "Efficient and Robust Approximate Nearest Neighbor Search Using Hierarchical Navigable Small World Graphs — Malkov & Yashunin (IEEE TPAMI 2018)"
    url: "https://arxiv.org/abs/1603.09320"
  - title: "Product Quantization for Nearest Neighbor Search — Jégou, Douze & Schmid (IEEE TPAMI 2011)"
    url: "https://inria.hal.science/inria-00514462"
  - title: "pgvector CHANGELOG (v0.8.2, 2026-02-25)"
    url: "https://github.com/pgvector/pgvector/blob/master/CHANGELOG.md"
  - title: "A Complete Guide to Filtering in Vector Search (Qdrant)"
    url: "https://qdrant.tech/articles/vector-search-filtering/"
  - title: "pgvector, a guide for DBA — Part 2: Indexes (dbi services, March 2026)"
    url: "https://www.dbi-services.com/blog/pgvector-a-guide-for-dba-part-2-indexes-update-march-2026/"
---

**Gist.** Semantic search, retrieval-augmented generation (RAG) and item-similarity recommendation all reduce to one query: given a vector, return the k closest vectors among N, where exact evaluation is a linear scan — 100M embeddings at 768 float32 dimensions is roughly 300 GB touched per query — and classic spatial indexes (KD-trees, R-trees) degenerate toward that same scan as dimensionality rises. Approximate nearest-neighbor (ANN) indexes answer a weaker question instead: return *most* of the true neighbors, quickly. The cost is that correctness becomes a tunable quantity — **recall**, the fraction of true neighbors returned — and every ANN structure exposes a knob trading recall against latency, memory, or build time.

## HNSW: a skip list made of graphs

Hierarchical Navigable Small World (HNSW) graphs, described by Malkov and Yashunin (arXiv 1603.09320, IEEE TPAMI 2018), are the structure behind Qdrant, Weaviate, Milvus, pgvector, and Lucene's HNSW codec used by Elasticsearch and OpenSearch.

The single-layer case first. Each vector is connected to its near neighbors plus a few longer-range links, and a query is answered by a **greedy graph walk**: starting from an entry point, repeatedly move to whichever neighbor is closest to the query vector, stopping when no neighbor improves on the current node. Small-world connectivity keeps the number of hops low, but a flat graph has two defects — the walk can terminate in a local minimum from which no single hop improves, and the early hops spend distance computations crossing the space rather than refining a candidate.

HNSW addresses both with the construction a skip list applies to a linked list. Each inserted vector is assigned a maximum layer drawn from an **exponentially decaying distribution**, so layer 0 contains every vector, layer 1 a small fraction, layer 2 a fraction of that, and so on. Search enters at the sparsest top layer and greedily walks to the closest node there, which costs few distance computations because few nodes exist at that layer while the individual hops span large distances. It then descends one layer, using the node found above as the entry point, and repeats. At layer 0 the search is no longer a pure greedy walk but a **beam search**: a bounded candidate list of size `ef` is maintained, so the traversal can pass through a node that is momentarily worse than the incumbent and still recover — this is what removes the local-minimum failure of the flat graph.

Two parameters govern the structure:

- **M** — the number of links retained per node per layer. Larger M raises recall and connectivity, and raises index size, because the graph stores up to M edges per node per layer — and the paper sets the ground layer's cap to roughly 2M, since layer 0 carries every vector. Construction is slower. The paper reports M in the range 5–48 as the useful span; pgvector's default is 16.
- **ef_construction / ef_search** — the beam width (candidate-list size) used at build time and at query time respectively. **`ef_search` is the runtime recall dial**: increasing it visits more nodes, drives recall toward the exact result, and increases latency roughly in step. It can be changed per query without touching the index; M cannot.

The costs HNSW imposes are structural. The graph is traversed by pointer-chasing, so it is kept **resident in RAM**; memory per vector is dimensions x 4 bytes for float32 storage plus the edge lists. Insertion is incremental and cheap. **Deletion is not**: removing a node would break the paths that route through it, so engines generally tombstone the entry, filter it out of results, and reclaim space by rebuilding.

## IVF and product quantization

**Inverted file (IVF).** k-means partitions the vectors into `nlist` cells, each with a centroid. A query is compared against the centroids and only the `nprobe` closest cells are scanned. Building and storing this is considerably cheaper than a graph, but recall degrades in a specific way: a true neighbor lying immediately across a cell boundary is missed unless its cell happens to be probed. The tuning dial is `nprobe` rather than `ef`.

**Product quantization (PQ)** (Jégou, Douze and Schmid, IEEE TPAMI 2011). Each vector is split into subvectors — for instance 96 of them — and each subvector is replaced by the index of the nearest of 256 learned centroids, one byte apiece. A 768-dimensional float32 vector occupying 3072 bytes becomes 96 bytes. Distances are then approximated from **precomputed centroid distance tables, without decompressing the vector**. PQ is a compression scheme rather than an index; composed with IVF as IVF-PQ, the standard Faiss configuration, it is how billion-vector collections are held in RAM, at the price of a further loss of recall — the 768-dimensional example above shrinks by 32x before the IVF and PQ codebooks are counted.

## The filtering problem

Production queries carry predicates: the nearest documents *where* `tenant_id = 7` and `lang = 'de'`. Two direct strategies each fail in a definite way.

- **Post-filtering** runs the ANN search and applies the predicate to the k results. With a predicate that admits 1% of the corpus, a top-100 result set may contain **zero survivors**; the system then returns fewer than k rows, or re-issues the search with a progressively larger k.
- **Pre-filtering** evaluates the predicate first and brute-forces the survivors. This is correct, and is a full scan whenever the surviving set is large. Restricting the HNSW walk to permitted nodes instead does not help either: the permitted subgraph can be **disconnected**, leaving qualifying neighbors unreachable from the entry point even though they exist.

Engines therefore plan between the two. Qdrant's filterable HNSW chooses per query between filtered graph traversal and a payload-index scan according to the cardinality of the filter. pgvector 0.8 added **iterative index scans**, in which the HNSW scan is resumed and continues yielding candidates until enough of them satisfy the `WHERE` clause. Versions checked at the time of writing: pgvector **0.8.2** (2026-02-25) on PostgreSQL 18.

```sql
CREATE EXTENSION vector;
CREATE TABLE docs (id bigserial PRIMARY KEY,
                   tenant_id int,
                   embedding vector(768));
CREATE INDEX ON docs USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);      -- build-time parameters

SET hnsw.ef_search = 100;                   -- recall dial (default 40)
SET hnsw.iterative_scan = relaxed_order;    -- keep filling k past the filter

SELECT id FROM docs
WHERE tenant_id = 7
ORDER BY embedding <=> $1                   -- <=> = cosine distance
LIMIT 10;
```

### Implementation sketch (Scala)

The load-bearing routine is the bounded beam search over one layer: a min-heap of candidates still to expand, a max-heap of the best `ef` results found so far, and the termination rule that stops as soon as the nearest unexpanded candidate is worse than the worst kept result.

```scala
type Id = Int

def searchLayer(
    query:      Array[Float],
    entry:      Id,
    ef:         Int,
    neighbours: Id => Array[Id],
    dist:       (Array[Float], Id) => Float
): Vector[(Float, Id)] =
  val d0 = dist(query, entry)
  // candidates: nearest first; results: farthest first, so the worst is O(1) to inspect
  val candidates = collection.mutable.PriorityQueue((d0, entry))(Ordering.by(-_._1))
  val results    = collection.mutable.PriorityQueue((d0, entry))(Ordering.by(_._1))
  val visited    = collection.mutable.HashSet(entry)

  while candidates.nonEmpty do
    val (dc, c) = candidates.dequeue()
    // every remaining candidate is at least as far as dc, so nothing can improve the beam
    if dc > results.head._1 && results.size >= ef then candidates.clear()
    else
      for n <- neighbours(c) if visited.add(n) do
        val dn = dist(query, n)
        if results.size < ef || dn < results.head._1 then
          candidates.enqueue((dn, n))
          results.enqueue((dn, n))
          if results.size > ef then results.dequeue()   // evict the farthest

  results.dequeueAll.reverse.toVector   // dequeueAll yields farthest first
```

The full search calls this with `ef = 1` on each layer above zero, feeding the single returned node in as the entry point for the layer below, and with `ef = ef_search` at layer 0.

## Where the retrieval layer sits

ANN indexes appear as the retrieval stage in three recurring designs: **semantic search** (corpus and query embedded, ANN retrieval followed by a reranker), **RAG** (the same retrieval feeding a language model, frequently hybridised with a [keyword inverted index](/articles/distributed-systems/2026-08-10-inverted-index-full-text-search) and reciprocal-rank fusion), and **recommendation** (a user vector queried against item vectors). The choice between exact scan, HNSW and IVF-PQ follows from corpus size and memory budget; the filtering strategy follows from predicate selectivity; and the recurring operational costs are RAM-resident graphs, rebuilds after deletion, and re-embedding when the model changes.

## Pitfalls

- Tuning `ef_search` without measuring recall reports latency improvements that are silent accuracy regressions. Recall@k is only observable against a ground truth, which requires an exact scan over a sampled query set with the index disabled.
- Deleting rows in bulk leaves an HNSW graph carrying tombstones: the walk still traverses the removed nodes and their distance computations, so latency stays high while the result set shrinks.
- A highly selective predicate combined with post-filtering returns fewer than k rows rather than an error, so the defect surfaces as thin results in the application rather than as a failed query.
- Restricting graph traversal to nodes passing a filter can disconnect the permitted subgraph, and the search then omits qualifying vectors it could not reach — a recall loss that does not appear in unfiltered benchmarks.
- Changing the embedding model changes the vector space; old and new vectors are not comparable, so the entire corpus must be re-embedded and the index rebuilt rather than migrated incrementally.
- Sizing a host from vector payload alone underestimates HNSW: the edge lists add up to M links per node per layer — about 2M at layer 0, which every vector occupies — on top of dimensions x 4 bytes, and the graph must remain in RAM for the traversal to perform as measured.
- Applying IVF's `nprobe` reasoning to HNSW, or the reverse, misconfigures both: `nprobe` selects how many partitions are scanned, `ef` selects how wide the beam is within a single connected structure.
