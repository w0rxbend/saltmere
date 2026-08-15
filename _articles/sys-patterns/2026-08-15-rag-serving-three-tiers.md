---
title: "RAG Serving: Retriever, Reranker, and LLM as Three Independently-Scaled Tiers"
date: 2026-08-15
track: sys-patterns
summary: "Retrieval-augmented generation is a serving topology, not a prompt trick. The retriever, the optional cross-encoder reranker, and the generator LLM have different resource profiles, and are scaled, placed, and latency-budgeted as three separate tiers."
reading_time: 7
tags: [rag, llm-serving, vector-search, reranker, vllm, retrieval]
sources:
  - title: "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks (Lewis et al., 2020)"
    url: "https://arxiv.org/abs/2005.11401"
  - title: "Hybrid Search with Qdrant's Query API"
    url: "https://qdrant.tech/articles/hybrid-search/"
  - title: "vLLM — Scoring / rerank usage for cross-encoder models"
    url: "https://docs.vllm.ai/en/stable/models/pooling_models/scoring/"
  - title: "BAAI/bge-reranker-v2-m3 (model card)"
    url: "https://huggingface.co/BAAI/bge-reranker-v2-m3"
  - title: "Best Reranker Models for RAG: Open-Source vs API (2026)"
    url: "https://docs.bswen.com/blog/2026-02-25-best-reranker-models/"
---

**Gist.** Retrieval-augmented generation (RAG) chains three stages — an approximate-nearest-neighbour retriever, an optional cross-encoder reranker, and a decoder-only large language model (LLM) — whose bottlenecks are respectively memory bandwidth, small-model compute, and high-bandwidth-memory (HBM) bandwidth on an accelerator. Deploying them as one process forces the three to scale in lockstep, so retrieval throughput can only be bought by adding graphics processing units (GPUs) that retrieval does not use. Splitting them into three services gives each its own scaling signal and placement, at the cost of three network hops, three failure domains, and one end-to-end latency budget that must be divided explicitly.

The RAG paper (Lewis et al., 2020) framed retrieval-augmented generation as a single model fine-tuned end to end: a dense-passage retriever feeding a sequence-to-sequence generator, with the query encoder and the generator trained together and the document index held fixed. Production deployments generally do not run it that way. What ships instead is a **pipeline of independent services**, and the engineering interest lies not in the prompt but in the fact that the three stages share nothing at the hardware level.

## Three tiers, three resource profiles

Tracing a single query through the stack shows the stressed resource change at every hop.

The **retriever** turns a query into candidates. Dense retrieval embeds the query once — one small forward pass — then runs an approximate-nearest-neighbour (ANN) search, either hierarchical navigable small world (HNSW) graph walks or inverted-file (IVF) probes, over millions of vectors. That search is bound by memory bandwidth and central-processing-unit (CPU) throughput; the hot index is held in random-access memory (RAM). Hybrid retrieval — Qdrant's Query API fuses dense vectors with sparse lexical matches such as BM25 or SPLADE — adds an inverted-index lookup and further input/output. **No large accelerator is involved.**

The **reranker** is an optional second pass, and the distinction that makes it expensive is the encoder architecture. The retriever uses a *bi-encoder*: query and document are embedded separately and offline, and scoring is a dot product over precomputed vectors, so document embedding cost is amortised across all queries. A *cross-encoder* reranker instead feeds the query and each candidate document **through the transformer together**, allowing token-level attention between the two, and emits a single relevance score. That score cannot be precomputed, because it depends on the pair. The consequence is the tier's defining cost shape: **one full forward pass per candidate, so latency is linear in shortlist length**, not constant as in the retriever. `BAAI/bge-reranker-v2-m3` is built on the multilingual `bge-m3` encoder, a few hundred million parameters rather than the billions of a generator. No published benchmark fixes its per-candidate latency across hardware, so the shortlist cost has to be measured on the target fleet rather than assumed. It is a small transformer: it wants *a* GPU or a large CPU, but nothing on the scale the generator requires.

The **generator** is the accelerator-bound tier. An 8B–70B decoder-only LLM performs prefill over the retrieved context, then autoregressive decode, bound by HBM bandwidth and key-value (KV) cache capacity. It is the tier that requires datacentre-class accelerators, and the serving stack around it — continuous batching, paged attention — exists to keep those accelerators busy.

| Tier | Work | Bound by | Hardware | Scale trigger |
|------|------|----------|----------|---------------|
| Retriever | ANN + lexical search | RAM bandwidth, CPU, I/O | CPU / RAM-heavy nodes | index size, QPS |
| Reranker | cross-encoder scoring | small-model compute | 1 modest GPU / large CPU | candidates × QPS |
| Generator | prefill + decode | HBM bandwidth, VRAM | large GPU(s) | concurrent decode streams |

## The argument for decoupling

The tiers saturate at different points. A knowledge-base assistant may field several hundred queries per second of retrieval while sustaining only tens of queries per second of generation, since most queries fan out to a handful of documents and the LLM is the rate-limiting stage. In a single process the scaling unit is the whole pipeline: doubling retrieval throughput doubles the GPU count as a side effect. As three services behind separate autoscalers, **each tier grows on its own signal** — index size and query rate for the retriever, candidate volume for the reranker, concurrent decode streams for the LLM.

Decoupling also permits **placement** per tier. The vector database runs on RAM-heavy CPU nodes near the data. The generator runs on scarce GPU nodes packed to high utilisation. The reranker can share the *same* GPU fleet as the LLM: vLLM serves cross-encoder and rerank models through its pooling `/score` and `/rerank` endpoints, so one server type can cover two tiers where a third deployment is unwanted.

The cost is three network hops instead of one, three failure domains, and a distributed latency budget that has to be defended.

## The latency budget across hops

Every hop consumes part of the p95. The table below is an illustrative allocation for an interactive answer, not a measurement: the constants depend entirely on index size, model and hardware, and what carries over between deployments is the ordering of the terms and how each one grows.

| Hop | Illustrative p95 | Note |
|-----|-------------|------|
| Query embed | 5–15 ms | one small forward pass |
| ANN + hybrid search | 10–40 ms | grows with index size / `ef_search` |
| Rerank top-100 | 50–100 ms | linear in candidates; the tuning knob |
| LLM time-to-first-token | 150–500 ms | prefill of retrieved context |
| LLM decode | 10–40 ms/token | streamed to the caller |

Time-to-first-token dominates, and retrieved context inflates it: every additional document placed in the prompt is more tokens to prefill. This is the lever the reranker pulls. Retrieve broadly — top-200 for recall — then let the cross-encoder cut to the **top-5 that earn a place in the context window**. The reranking pass buys a shorter, higher-signal prompt and a correspondingly cheaper prefill, and the trade is worthwhile whenever the reranker's own latency is smaller than the prefill time it removes. Omitting the reranker leaves two options: pay for the longer prompt, or accept weaker grounding.

The invariant that makes the shape work is that **recall is set at the retriever and precision at the reranker**. A document the ANN search never returns cannot be recovered downstream, so the retriever's `limit` is a recall floor; the reranker's `top_n` is a precision-versus-prefill dial applied to whatever that floor admitted.

## Wiring the pipeline

Three clients, one function, each tier independently addressable:

```python
from qdrant_client import QdrantClient
import requests

qdrant = QdrantClient(url="http://retriever:6333")
RERANK = "http://reranker:8000/rerank"      # vLLM pooling server
LLM    = "http://generator:8001/v1/chat/completions"

def answer(query: str, q_vec: list[float]) -> str:
    # Tier 1: retrieve broadly (CPU/RAM node) — sets the recall floor
    hits = qdrant.query_points(
        collection_name="docs", query=q_vec, limit=200,
    ).points
    docs = [h.payload["text"] for h in hits]

    # Tier 2: cross-encoder rerank, keep the top 5 (small GPU)
    scored = requests.post(RERANK, json={
        "model": "BAAI/bge-reranker-v2-m3",
        "query": query, "documents": docs, "top_n": 5,
    }).json()["results"]
    context = "\n\n".join(docs[r["index"]] for r in scored)

    # Tier 3: generate grounded answer (large GPU)
    resp = requests.post(LLM, json={
        "model": "meta-llama/Llama-3.1-8B-Instruct",
        "messages": [
            {"role": "system", "content": f"Answer only from:\n{context}"},
            {"role": "user", "content": query},
        ],
    }).json()
    return resp["choices"][0]["message"]["content"]
```

Each URL addresses a deployment scaled on its own metric. Substituting a different vector database, a different reranker such as Cohere Rerank 3.5, or a larger generator leaves the tier boundaries where they are. That is what treating RAG as a serving pattern means: three stages, three resource profiles, three autoscalers, one latency budget spent deliberately.

Instrumenting each hop separately over a representative query set shows where the p95 is spent; reducing the reranker's `top_n` then trades prefill time and cost per answer against grounding quality, and the two effects can be measured independently.

## Pitfalls

- **Sharing one process across tiers makes the GPU the scaling unit for retrieval.** Retrieval throughput can then only be raised by adding accelerators that perform no retrieval work; utilisation on those accelerators falls while the bill rises.
- **Rerank latency scales linearly with shortlist size, not with the final `top_n`.** Raising the retriever's `limit` from 100 to 200 to improve recall doubles reranker time even though the prompt is unchanged, because the cross-encoder runs one forward pass per candidate.
- **A document missed by the ANN search is unrecoverable.** Tuning `ef_search` or the probe count down to shave retrieval milliseconds silently lowers the recall ceiling; the reranker cannot score a candidate it never received.
- **Cross-encoder scores are not comparable across queries.** They are produced for a specific query-document pair, so a fixed absolute score threshold behaves inconsistently between queries; ranking within a query is the supported use.
- **Enlarging the context window shifts cost to prefill, which dominates time-to-first-token.** Adding documents raises time-to-first-token in a way that no decode-side optimisation recovers.
- **Colocating the reranker on the generator's GPU fleet couples their failure and saturation behaviour.** A surge in candidate volume then competes for the same accelerator that serves decode streams, so two independent scale triggers contend for one resource.
