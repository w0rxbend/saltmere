---
title: "Semantic Caching for LLM Serving: Reusing Answers by Meaning, Not Exact Match"
date: 2026-08-14
track: sys-patterns
summary: "Embed the incoming prompt, run a vector-similarity search against cached prompt-to-response pairs, and return a stored answer when the score clears a threshold. It sits in front of the LLM gateway and trades a real false-positive risk for latency and cost."
reading_time: 6
tags: [llm-serving, semantic-cache, gptcache, redis, embeddings, vector-search, ai-infrastructure]
sources:
  - title: "GPTCache — Semantic cache for LLMs (zilliztech, GitHub README)"
    url: "https://github.com/zilliztech/gptcache/blob/main/README.md"
  - title: "GPTCache: An Open-Source Semantic Cache for LLM Applications (paper, OpenReview)"
    url: "https://openreview.net/pdf?id=ivwM8NwM4Z"
  - title: "Redis LangCache — semantic caching service (Redis docs)"
    url: "https://redis.io/docs/latest/develop/ai/context-engine/langcache/"
  - title: "Cache LLM Responses — RedisVL SemanticCache user guide"
    url: "https://docs.redisvl.com/en/latest/user_guide/03_llmcache.html"
  - title: "Semantic Caching for LLMs: Beyond Prefix Caching (TrueFoundry)"
    url: "https://www.truefoundry.com/blog/semantic-caching-ai-gateway"
---

Two users ask "How do I roll back a Postgres migration?" and "what's the way to undo a postgres migration?" — same answer, zero byte overlap. Exact-match caching hashes the normalized request and misses both. Prefix caching (the KV-reuse pattern) only helps when the *tokens* at the front of the prompt are identical; it does nothing for reworded questions. **Semantic caching** closes that gap by keying the cache on *meaning*.

The mechanism is a small pipeline in front of your LLM gateway: embed the incoming prompt into a vector, run a nearest-neighbor search against previously cached `(prompt → response)` pairs, and if the closest hit clears a similarity threshold, return the stored response without ever touching the model. On a hit, a several-hundred-millisecond model call collapses to tens of milliseconds.

## Where it sits, and how it differs

Keep the three caching layers straight — they compose, they don't compete:

- **Exact-match response cache**: hash the whole request. Safe, near-zero false positives, low hit rate.
- **Prefix / KV cache** (PagedAttention, vLLM): reuse attention state for identical leading tokens. Cuts prefill cost *inside* the engine; still runs the model.
- **Semantic cache**: vector similarity over prompts. Highest hit rate, and the only layer that carries a genuine correctness risk.

Semantic caching lives at the gateway, *before* the request reaches any model replica. That placement is what lets it short-circuit the whole call.

## A concrete cache with GPTCache

[GPTCache](https://github.com/zilliztech/gptcache/blob/main/README.md) (from Zilliz, the Milvus team) wraps the OpenAI adapter and checks the cache before forwarding. It plugs together an embedding function, a vector store for similarity search, and a scalar store for the responses:

```python
from gptcache import cache
from gptcache.adapter import openai
from gptcache.embedding import Onnx
from gptcache.manager import CacheBase, VectorBase, get_data_manager
from gptcache.similarity_evaluation.distance import SearchDistanceEvaluation

onnx = Onnx()  # local embedding model, no API round-trip
data_manager = get_data_manager(
    CacheBase("sqlite"),                       # stores prompts + responses
    VectorBase("faiss", dimension=onnx.dimension),  # ANN index over embeddings
)
cache.init(
    embedding_func=onnx.to_embeddings,
    data_manager=data_manager,
    similarity_evaluation=SearchDistanceEvaluation(),
)
cache.set_openai_key()

# Drop-in: same call signature, cache checked first
resp = openai.ChatCompletion.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "How do I undo a Postgres migration?"}],
)
```

The reworded twin now embeds close to the stored vector, clears the distance check, and returns the cached answer. GPTCache supports LRU/LFU/FIFO eviction and swaps FAISS for Milvus/Chroma/PGVector when you outgrow one box.

## Tuning the threshold — the whole game

The similarity threshold is a precision/recall dial with no universal value. A workable starting point and the ranges TrueFoundry publishes:

- **0.95–1.0** — high-stakes routes where a wrong answer is costly.
- **0.85–0.95** — general conversational assistants; start around **0.9** and adjust.
- **< 0.85** — exploratory, low-risk lookups.

Set it *per route*, not globally. Then move it based on the observed false-hit rate, not vibes.

## The false-positive trap

The failure mode is blunt: **embedding-close is not meaning-equal.** "What is the capital of France?" and "What is the capital of Germany?" sit millimeters apart in embedding space and demand different answers. A too-loose threshold will confidently serve Paris for Berlin. Mitigations that actually hold:

- **Conservative thresholds** on anything factual.
- **Entity/keyword guards** — verify salient tokens (names, IDs, numbers) match before accepting a hit.
- **Per-tenant / per-namespace caches** so one customer's context never satisfies another's query.

Never semantically cache personalized, time-sensitive, or stateful responses without explicit per-user scoping.

## Eviction and TTL

Cached answers go stale two ways. **Time-based TTL** should track how fast the underlying data changes — minutes for pricing, days for documentation. **Version-based invalidation** bumps a namespace when the system prompt or tool schema changes, rolling every entry forward so old configs can't leak through. Managed options like [Redis LangCache](https://redis.io/docs/latest/develop/ai/context-engine/langcache/) (currently in preview) expose threshold, TTL, and eviction as service config and generate embeddings for you; [RedisVL's `SemanticCache`](https://docs.redisvl.com/en/latest/user_guide/03_llmcache.html) gives you the same primitives self-hosted.

Measure the hit rate and the false-hit rate together. A 60% hit rate that serves Berlin-for-Paris 2% of the time is worse than a 40% hit rate that never lies.

**Try next:** Stand up GPTCache with FAISS in front of one endpoint, replay a day of real prompts, and sweep the threshold from 0.80 to 0.98 — plot hit rate and manually-audited false-hit rate at each step to find your route's knee.
