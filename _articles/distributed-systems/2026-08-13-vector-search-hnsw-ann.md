---
title: "Vector search at scale: HNSW and the approximate nearest-neighbor toolbox"
date: 2026-08-13
track: distributed-systems
summary: "Exact kNN over 100M embeddings is a full scan per query, so every real vector store approximates. HNSW's skip-list-of-graphs, the M / ef knobs, IVF and product quantization in a paragraph each, and the filtering problem that ambushes RAG system-design interviews — with a runnable pgvector 0.8.2 example."
reading_time: 6
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

Semantic search, RAG retrieval, and "users also liked" all reduce to the same query: given a vector, find the k closest vectors among N. Exact kNN is a linear scan — 100M embeddings at 768 float32 dims is ~300 GB touched per query — and classic spatial indexes (KD-trees, R-trees) collapse toward that same scan in high dimensions, the curse of dimensionality. So every production system answers a weaker question: find *most* of the true neighbors, fast. The fraction you find is **recall**, and recall-vs-latency is the dial every ANN algorithm exposes.

## HNSW: a skip list made of graphs

Hierarchical Navigable Small World graphs (Malkov & Yashunin, 2016/2018) are the default answer in 2026 — they power Qdrant, Weaviate, Milvus, pgvector, and Lucene's HNSW codec behind Elasticsearch and OpenSearch.

Start with the one-layer idea: connect each vector to its near neighbors (plus a few long-range links) and answer queries by **greedy graph walk** — from an entry point, repeatedly move to the neighbor closest to the query until no neighbor improves. Small-world structure makes the hop count roughly logarithmic, but a flat graph can get stuck in local minima and wastes early hops crossing the space.

HNSW fixes this exactly the way a skip list fixes a linked list. Each vector is assigned a maximum layer drawn from an exponentially decaying distribution, so layer 0 holds everything, layer 1 a small fraction, layer 2 a fraction of that. A search enters at the sparse top layer, greedily walks to the closest node there — long hops across the space — then drops a layer and refines, until layer 0 does the fine-grained search with a beam of candidates. Two parameters matter:

- **M** — links per node per layer. Higher M: better recall and connectivity, more memory (index size scales with it), slower construction. Typical 12–48.
- **ef_construction / ef_search** — beam width (candidate-list size) at build and query time. `ef_search` is *the* runtime recall dial: raise it, recall climbs toward exact at the cost of latency.

HNSW's costs: the graph lives in RAM for speed, inserts are cheap but deletes are awkward (most engines tombstone and rebuild), and memory per vector is dims x 4 bytes plus ~M links.

## IVF and PQ, one paragraph each

**IVF (inverted file):** run k-means to partition vectors into `nlist` cells; at query time, probe only the `nprobe` closest cells and scan those. It's clustering-as-index — much cheaper to build and store than a graph, but recall suffers when true neighbors sit just across a cell boundary, and you tune the nprobe/latency dial instead of ef.

**Product quantization (Jégou, Douze & Schmid, 2011):** compress each vector by splitting it into (say) 96 subvectors and quantizing each subvector to one of 256 learned centroids — a 768-dim, 3072-byte float32 vector becomes 96 bytes, and distances are approximated from precomputed centroid tables without decompression. PQ is a *compression* scheme, not an index; combined as IVF-PQ (the Faiss workhorse) it's how billion-vector datasets fit in RAM, trading a further recall haircut for ~30x memory savings.

## The filtering problem

Real queries are never pure: "nearest docs *where tenant_id = 7 and lang = 'de'*". Two naive strategies both fail:

- **Post-filter:** ANN-search first, filter the k results after. With a 1%-selective filter, your top-100 may contain zero survivors — you return fewer than k or re-query with growing k.
- **Pre-filter:** filter first, brute-force the survivors. Fine for tiny result sets, a full scan otherwise; and restricting an HNSW walk to allowed nodes can disconnect the graph so greedy search can't reach them.

Engines now build hybrids — Qdrant's filterable HNSW plans per-query between filtered graph traversal and payload-index scans depending on filter cardinality. pgvector 0.8 added **iterative index scans** for the same hole: the HNSW scan resumes and keeps yielding candidates until the `WHERE` clause has enough survivors. Versions checked as of writing: pgvector **0.8.2** (2026-02-25), running the example below on Postgres 18.

```sql
CREATE EXTENSION vector;
CREATE TABLE docs (id bigserial PRIMARY KEY,
                   tenant_id int,
                   embedding vector(768));
CREATE INDEX ON docs USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);      -- build-time knobs

SET hnsw.ef_search = 100;                   -- recall dial (default 40)
SET hnsw.iterative_scan = relaxed_order;    -- keep filling k past the filter

SELECT id FROM docs
WHERE tenant_id = 7
ORDER BY embedding <=> $1                   -- <=> = cosine distance
LIMIT 10;
```

## Where interviews go with this

ANN shows up as the retrieval layer in three standard designs: **semantic search** (embed corpus + query, ANN + rerank), **RAG** (same retrieval, feeding an LLM — often hybrid with a [keyword inverted index](/articles/distributed-systems/2026-08-13-inverted-index-full-text-search) and reciprocal-rank fusion), and **recommendations** (user vector against item vectors). Strong answers name the trade-offs concretely: exact scan vs HNSW vs IVF-PQ by corpus size and memory budget; ef_search as an SLO knob (measure recall@k against a brute-force sample, don't guess); filtering strategy chosen by filter selectivity; and operational costs — RAM-resident graphs, rebuild-on-delete, and re-embedding the corpus when the model changes.

**Try next:** load 100k random vectors into pgvector, and plot recall@10 (against exact `ORDER BY` with the index disabled) as you sweep `hnsw.ef_search` from 10 to 400 — the knee of that curve is the whole ANN trade in one picture.
