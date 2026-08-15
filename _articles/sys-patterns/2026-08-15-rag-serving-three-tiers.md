---
title: "RAG Serving: Retriever, Reranker, and LLM as Three Independently-Scaled Tiers"
date: 2026-08-15
track: sys-patterns
summary: "Retrieval-augmented generation is a serving topology, not a prompt trick. The retriever, the optional cross-encoder reranker, and the generator LLM have wildly different resource profiles — treat them as three tiers you scale, place, and budget latency for separately."
reading_time: 6
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

The RAG paper (Lewis et al., 2020) framed retrieval-augmented generation as one differentiable model: a retriever and a seq2seq generator trained together. In production almost nobody runs it that way. What ships instead is a **pipeline of independent services**, and the interesting engineering is not the prompt — it's that the three stages have nothing in common at the hardware level. Bolt them into one process and you scale your cheapest bottleneck with your most expensive silicon.

## Three tiers, three resource profiles

Walk a single query through the stack and watch the resource it stresses change at every hop.

The **retriever** turns a query into candidates. Dense retrieval embeds the query once (a small forward pass) and runs an approximate-nearest-neighbour (ANN) search — HNSW graph walks or IVF probes — over millions of vectors. That search is memory-bandwidth and CPU bound; the hot index wants to live in RAM. Hybrid retrieval (Qdrant's Query API fuses dense vectors with sparse BM25/SPLADE lexical matches) adds an inverted-index lookup, more I/O still. No big GPU required.

The **reranker** is an optional second pass. Where the retriever used a *bi-encoder* — query and document embedded separately, scored by a cheap dot product — a cross-encoder reranker feeds the query and each candidate document *together* through a transformer and emits a relevance score. That is far more accurate and far more expensive: it's O(candidates) full forward passes. `BAAI/bge-reranker-v2-m3` is ~568M parameters; it reranks a shortlist of 50–100 docs in roughly 50–100 ms on a GPU (200–400 ms on CPU). It's a small transformer — it wants *a* GPU or a fat CPU, but nothing like the generator.

The **generator** is the GPU glutton. An 8B–70B decoder-only LLM does prefill over the retrieved context then autoregressive decode, bound by HBM bandwidth and KV-cache capacity. This is the tier you run on H100/H200-class accelerators with continuous batching and paged attention.

| Tier | Work | Bound by | Hardware | Scale trigger |
|------|------|----------|----------|---------------|
| Retriever | ANN + lexical search | RAM bandwidth, CPU, I/O | CPU / RAM-heavy nodes | index size, QPS |
| Reranker | cross-encoder scoring | small-model compute | 1 modest GPU / big CPU | candidates × QPS |
| Generator | prefill + decode | HBM bandwidth, VRAM | big GPU(s) | concurrent decode streams |

## Why decoupling wins

Because the tiers saturate at different points. A knowledge-base assistant might field 500 QPS of retrieval but only 40 QPS of generation — most queries fan out to a handful of docs, and the LLM is the slow, rate-limiting stage. If retriever, reranker, and generator share a process, you can only scale in lockstep: to double retrieval throughput you double GPU count you didn't need. Split into three services behind their own autoscalers and each grows on its own signal — index size and QPS for the retriever, candidate volume for the reranker, concurrent decode streams for the LLM.

Decoupling also lets you **place** each tier where it's cheap. The vector DB runs on RAM-heavy CPU nodes near the data. The generator runs on scarce GPU nodes you pack to high utilization. The reranker can even ride the *same* GPU fleet as the LLM — vLLM serves cross-encoder/rerank models through its pooling `/score` and `/rerank` endpoints, so one server type covers two tiers if you'd rather not run a third deployment.

The trade-off is real: three network hops instead of one, three failure domains, and a distributed latency budget to defend. Which is the thing to design around.

## The latency budget across hops

Every hop spends part of your p95. A rough budget for an interactive answer:

| Hop | Typical p95 | Note |
|-----|-------------|------|
| Query embed | 5–15 ms | one small forward pass |
| ANN + hybrid search | 10–40 ms | grows with index size / `ef_search` |
| Rerank top-100 | 50–100 ms | linear in candidates; the tuning knob |
| LLM time-to-first-token | 150–500 ms | prefill of retrieved context |
| LLM decode | 10–40 ms/token | streamed to the user |

Time-to-first-token dominates, and retrieved context inflates it: every extra doc you stuff into the prompt is more tokens to prefill. That's the lever the reranker pulls — retrieve broadly (top-200 for recall), then let the cross-encoder cut to the **top-5 that actually earn a place in the context window**. You trade 60 ms of reranking for a shorter, higher-signal prompt and a faster, cheaper generate. Skip reranking and you either pay for a longer prompt or accept worse grounding.

## Wiring the pipeline

Three clients, one function, each tier independently addressable:

```python
from qdrant_client import QdrantClient
import requests

qdrant = QdrantClient(url="http://retriever:6333")
RERANK = "http://reranker:8000/rerank"      # vLLM pooling server
LLM    = "http://generator:8001/v1/chat/completions"

def answer(query: str, q_vec: list[float]) -> str:
    # Tier 1: retrieve broadly (CPU/RAM node)
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

    # Tier 3: generate grounded answer (big GPU)
    resp = requests.post(LLM, json={
        "model": "meta-llama/Llama-3.1-8B-Instruct",
        "messages": [
            {"role": "system", "content": f"Answer only from:\n{context}"},
            {"role": "user", "content": query},
        ],
    }).json()
    return resp["choices"][0]["message"]["content"]
```

Each URL points at a deployment you scale on its own metric. Swap Qdrant for another vector DB, the reranker for Cohere Rerank 3.5, or the generator for a bigger model — the tier boundaries don't move. That's the whole point of treating RAG as a serving pattern: three stages, three resource profiles, three autoscalers, one latency budget to spend deliberately.

**Try next:** put a stopwatch around each hop in the function above, run 100 real queries, and plot where p95 actually goes — then drop the reranker's `top_n` from 5 to 3 and watch prefill time (and cost per answer) fall while you check whether grounding quality holds.
