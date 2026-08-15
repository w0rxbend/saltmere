---
title: 'Semantic caching for LLMs: cache by meaning, not by key'
date: 2026-08-10
track: microservices
summary: Exact-match caching whiffs on LLM traffic because 'reset my password' and 'how do I change my password?' are different strings. A semantic cache embeds the query, does an ANN lookup in a vector store, and serves a cached answer when similarity clears a threshold. Here's the architecture, the false-hit trap, and how GPTCache and Redis LangCache build it.
reading_time: 6
tags:
- llm
- semantic-cache
- caching
- vector-database
- gptcache
- redis
- embeddings
- llm-serving
- vector-search
- ai-infrastructure
sources:
- title: GPTCache — Semantic cache for LLMs (README)
  url: https://github.com/zilliztech/gptcache/blob/main/README.md
- title: GPTCache documentation
  url: https://gptcache.readthedocs.io/
- title: Semantic Caching for LLMs — RedisVL user guide (SemanticCache)
  url: https://redis.io/docs/latest/develop/ai/redisvl/0.7.0/user_guide/llmcache/
- title: Introducing LangCache and vector sets — Redis blog
  url: https://redis.io/blog/spring-release-2025/
- title: Semantic caching thresholds and why they matter — Portkey
  url: https://portkey.ai/blog/semantic-caching-thresholds/
- title: 'GPTCache: An Open-Source Semantic Cache for LLM Applications (paper, OpenReview)'
  url: https://openreview.net/pdf?id=ivwM8NwM4Z
- title: Redis LangCache — semantic caching service (Redis docs)
  url: https://redis.io/docs/latest/develop/ai/context-engine/langcache/
- title: Cache LLM Responses — RedisVL SemanticCache user guide
  url: https://docs.redisvl.com/en/latest/user_guide/03_llmcache.html
- title: 'Semantic Caching for LLMs: Beyond Prefix Caching (TrueFoundry)'
  url: https://www.truefoundry.com/blog/semantic-caching-ai-gateway
---

A classic cache is a hash map: exact key in, value out. That contract is perfect for `GET /user/42` and useless for a chat model. Ask an LLM "how do I reset my password?" and then "what's the way to change my password?" — two different strings, one intent, two full-price API calls. Byte-for-byte keying gives you a near-zero hit rate on natural-language traffic, because humans almost never phrase the same question the same way twice.

**Semantic caching** fixes the key. Instead of hashing the raw prompt, you embed it into a vector, search a vector store for the nearest previously-seen query, and if the nearest neighbor is *close enough* you return its stored response without ever calling the model. The cache is keyed by meaning. That single change flips the hit rate from "basically never" to a large fraction of production traffic, and every hit skips both the token bill and the multi-second generation latency.

This is a different animal from the KV/prefix caching covered in [Prefix caching: the KV-reuse pattern for LLM serving](/articles/sys-patterns/2026-07-31-llm-prefix-caching-kv-reuse). Prefix caching lives *inside* the serving engine, reuses attention key/value tensors token-by-token, and requires an exact leading-token match — it speeds up a call you're still making. Semantic caching lives *in front of* the model, operates on whole request/response pairs, matches by fuzzy meaning, and lets you skip the call entirely. Token-level vs response-level; exact vs approximate. They compose: a semantic miss falls through to a model that still prefix-caches its system prompt.

## The architecture

Every semantic cache is the same four-stage pipeline:

1. **Embedding model.** Turn the incoming prompt into a dense vector — a sentence-transformer, an OpenAI embedding, an ONNX model, or a purpose-built cache embedding like Redis's `redis/langcache-embed-v1`. The embedding model *is* your notion of "similar"; a weak one collapses distinct questions together.
2. **Vector store + ANN search.** Store past query vectors and their responses, and on each request run an approximate-nearest-neighbor lookup to find the top-k closest. Exact nearest-neighbor over millions of vectors is too slow, so everyone uses ANN indexes (HNSW, IVF) that trade a sliver of recall for sub-millisecond search. FAISS, Milvus, Chroma, Qdrant, and Redis all qualify.
3. **Similarity threshold.** The nearest neighbor comes back with a distance (or similarity) score. Compare it to a threshold. Inside the band → **hit**, return the cached response. Outside → **miss**, call the LLM and write the new pair back into the store.
4. **Cache storage + eviction.** The actual response text and metadata live somewhere durable (SQLite, Postgres, Redis), governed by TTL and eviction so stale answers age out.

The whole thing is a read-through cache; the only exotic part is that step 3 is a *fuzzy* comparison, and that fuzziness is where all the risk lives.

## The false-hit trap and how to tune it

Exact-match caches cannot return a wrong answer — the key either matches or it doesn't. Semantic caches can, and this is the defining failure mode. Set the threshold too permissive and the cache confidently serves the response for a *different* question. "What's the capital of Australia?" pulls back the cached answer for "What's the capital of Austria?" — high embedding similarity, completely wrong fact.

The knob is a genuine precision/recall trade-off, and the published numbers are worth memorizing. Portkey's write-up cites AWS testing where a strict 0.99 cosine-similarity gate caught only obvious duplicates: **23.5% hit rate, 15.8% cost savings**. Loosening to 0.75 pushed cost savings to **86.3%** while accuracy fell only from 92.1% to 91.2% — a near-negligible quality hit for a 5x cost win in that workload. The practical playbook:

- **Start in the 0.90–0.95 similarity band.** Portkey recommends ~0.95, backtested on ~5,000 real queries to keep accuracy above 99%.
- **Build a validation set** of same-intent pairs (should hit) and similar-but-different pairs (must miss). Lower the threshold incrementally and watch the false-positive rate.
- **Stop when false positives hit ~3–5%.** Past that point the embedding model, not the threshold, is your ceiling — swap in a stronger embedder rather than tightening further.
- **Go stricter in high-stakes domains.** In medicine, law, or anything where "under 18" vs "under 80" flips the answer, small wording differences change meaning, so accept a lower hit rate for safety.

One direction gotcha: some libraries score **distance** (lower = closer, so the threshold is a *ceiling*) and others score **similarity** (higher = closer, a *floor*). Redis's `SemanticCache` uses `distance_threshold` defaulting to `0.1`; raise it to widen the net. Always confirm which convention you're on before you ship.

## GPTCache: the batteries-included version

[GPTCache](https://github.com/zilliztech/gptcache) (from Zilliz, the Milvus team) is the reference open-source implementation, and its module boundaries map exactly onto the four stages: an **LLM adapter** (OpenAI, LangChain, llama.cpp, and more), an **embedding generator**, a **vector store**, **cache storage**, and a **similarity evaluator**. The adapter is the clever bit — it wraps the OpenAI client so existing code keeps calling `openai.ChatCompletion.create` and the cache slots in transparently:

```python
from gptcache import cache
from gptcache.adapter import openai
from gptcache.embedding import Onnx
from gptcache.manager import CacheBase, VectorBase, get_data_manager
from gptcache.similarity_evaluation.distance import SearchDistanceEvaluation

onnx = Onnx()  # local ONNX embedding model
data_manager = get_data_manager(
    CacheBase("sqlite"),                              # response storage
    VectorBase("faiss", dimension=onnx.dimension),   # ANN index
)

cache.init(
    embedding_func=onnx.to_embeddings,
    data_manager=data_manager,
    similarity_evaluation=SearchDistanceEvaluation(),
)
cache.set_openai_key()

# Now this call checks the cache first, only hitting OpenAI on a miss:
resp = openai.ChatCompletion.create(
    model="gpt-3.5-turbo",
    messages=[{"role": "user", "content": "How do I reset my password?"}],
)
```

`SearchDistanceEvaluation` scores the ANN result, and GPTCache's `Config(similarity_threshold=...)` decides hit vs miss. Swap `VectorBase("faiss", ...)` for Milvus or Qdrant to scale out, or `Onnx()` for a stronger embedder — the adapter and evaluator stay put.

## Redis: LangCache and vector sets

Redis ships two layers. **RedisVL's `SemanticCache`** is the DIY object: point it at a Redis URL and an embedding vectorizer, then use `store(prompt, response)` and `check(prompt)`:

```python
from redisvl.extensions.cache.llm import SemanticCache
from redisvl.utils.vectorize import HFTextVectorizer

llmcache = SemanticCache(
    name="llmcache",
    redis_url="redis://localhost:6379",
    distance_threshold=0.1,                                   # tune this
    vectorizer=HFTextVectorizer("redis/langcache-embed-v1"),
)
llmcache.set_ttl(3600)                                        # staleness control

if hit := llmcache.check(prompt="How do I change my password?"):
    answer = hit[0]["response"]
else:
    answer = call_llm(prompt)
    llmcache.store(prompt="How do I change my password?", response=answer)
```

`set_threshold()` retunes live, `set_ttl()` handles eviction, and `filterable_fields` scope entries per user so the cache never leaks one tenant's answer to another. Underneath, Redis 8's **vector sets** — a native similarity data type from Redis's original author, modeled on sorted sets but pairing elements with vectors instead of scores — provide the ANN engine. Above it, **LangCache** is Redis's fully-managed REST service: you POST a query, it embeds, searches, and returns an approved cached response with configurable threshold and built-in per-user governance, no vector DB or invalidation logic to run yourself.

## Where it helps, where it hurts

Semantic caching earns its keep on **high-volume, repetitive, tolerant** traffic: FAQ and support bots, documentation Q&A, RAG front doors where thousands of users ask the same handful of questions in a thousand phrasings. Those workloads see the 70–90% cost reductions the vendors advertise.

It hurts, or is outright dangerous, when:

- **Answers are personalized or stateful.** "What's *my* account balance?" must never hit a neighbor's cached answer — scope with per-user filters or don't cache it at all.
- **Freshness matters.** Prices, weather, inventory: aggressive TTLs, or skip the cache.
- **Small wording flips meaning.** Legal/medical/financial prompts need strict thresholds, eroding the hit rate you were caching for.
- **Traffic is naturally unique.** Long, distinct code-gen or creative prompts rarely repeat; you pay embedding + search overhead for a hit rate near zero.

Treat the threshold as a product decision, not a constant — it's the dial between "cheap and occasionally wrong" and "correct and rarely cached," and only your validation set can tell you where to leave it.

**Try next:** stand up the GPTCache snippet against a local model, build a 50-pair validation set (25 same-intent, 25 near-miss), and sweep the similarity threshold from 0.80 to 0.98 — plot hit rate against false-positive rate and find your own knee in the curve.
