---
title: 'Semantic caching for LLMs: cache by meaning, not by key'
date: 2026-08-10
track: microservices
summary: Exact-match caching fails on large language model traffic because 'reset my password' and 'how do I change my password?' are different strings. A semantic cache embeds the query, runs an approximate-nearest-neighbour lookup in a vector store, and serves a cached answer when similarity clears a threshold. The architecture, the false-hit failure mode, and the implementations in GPTCache and Redis LangCache.
reading_time: 8
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

**Gist.** A conventional cache keys on exact bytes, so natural-language traffic to a large language model (LLM) misses almost always: humans rarely phrase the same question twice the same way. A semantic cache replaces the equality test with an embedding plus an approximate-nearest-neighbour (ANN) search, and returns a stored response when the nearest neighbour's similarity clears a threshold. **The cost is that the lookup is no longer sound** — an exact-match cache cannot return a wrong value, whereas a semantic cache can serve the answer to a different question, and the threshold that governs this is a precision/recall dial with no correct constant.

## Why byte keying fails here

A classic cache is a hash map: exact key in, value out. That contract fits `GET /user/42` and does not fit a chat model. "How do I reset my password?" and "What's the way to change my password?" are two distinct strings with one intent, so byte-for-byte keying charges two full-price API calls. The hit rate under byte keying is bounded by the rate of literal prompt repetition, which on free-text traffic is near zero.

Semantic caching changes the key rather than the cache. The prompt is embedded into a dense vector, the vector store is searched for the nearest previously-seen query, and if that neighbour is close enough the stored response is returned without invoking the model. A hit avoids both the token bill and the generation latency of the call.

This is distinct from the key/value prefix caching covered in [Prefix caching: the KV-reuse pattern for LLM serving](/articles/sys-patterns/2026-07-31-llm-prefix-caching-kv-reuse). Prefix caching lives *inside* the serving engine, reuses attention key/value tensors token by token, and requires an exact leading-token match; it accelerates a call that is still made. Semantic caching lives *in front of* the model, operates on whole request/response pairs, matches approximately, and can elide the call. The two compose: a semantic miss falls through to an engine that still prefix-caches its system prompt.

## The four-stage pipeline

1. **Embedding model.** The incoming prompt becomes a dense vector — a sentence transformer, a hosted embedding endpoint, a local ONNX model, or a cache-specific embedder such as Redis's `redis/langcache-embed-v1`. **The embedding model defines the system's notion of "similar";** a weak embedder collapses distinct questions into neighbouring vectors, and no threshold can separate them afterwards.
2. **Vector store and ANN search.** Past query vectors and their responses are stored, and each request runs an approximate-nearest-neighbour lookup for the top-k closest entries. Exact nearest-neighbour search over large corpora is too slow, so ANN indexes (HNSW, IVF) are used, **trading recall for latency**: a miss caused by index recall is indistinguishable at the call site from a genuine absence. FAISS, Milvus, Chroma, Qdrant and Redis all serve this role.
3. **Similarity threshold.** The nearest neighbour returns with a distance or similarity score, compared against a threshold. Inside the band → **hit**, the cached response is returned. Outside → **miss**, the model is called and the new pair is written back.
4. **Cache storage and eviction.** Response text and metadata live in a durable store (SQLite, Postgres, Redis) governed by time-to-live (TTL) and eviction so that stale answers age out.

The structure is a read-through cache. The only unusual element is that stage 3 is a fuzzy comparison, and the fuzziness carries all of the risk.

## The false-hit failure mode

An exact-match cache has a two-valued key test and therefore cannot return a wrong answer. A semantic cache can. With a permissive threshold, "What is the capital of Australia?" retrieves the cached answer for "What is the capital of Austria?" — high embedding similarity, wrong fact, and no signal at the call site that anything went wrong. **A false hit is silent**: it is indistinguishable from a correct hit without an independent check of the answer.

Published measurements on the trade-off are limited. Portkey's write-up reports a threshold sweep in which a strict 0.99 cosine-similarity gate caught only near-duplicates, yielding a **23.5% hit rate and 15.8% cost saving**; relaxing the gate to 0.75 raised the hit rate to 90.3% and the cost saving to **86.3%** while accuracy fell from 92.1% to 91.2%. That result is one workload, not a general law — the same relaxation on a corpus of minimally-contrasting prompts would degrade accuracy far more.

Reported operating practice:

- **Portkey recommends approximately 0.95 similarity**, a figure it attributes to experience across more than 250 million cache requests, and reports around 99% user-rated accuracy at that setting. It separately suggests a validation set of roughly 5,000 queries and a false-positive tolerance in the region of 3–5%.
- **A validation set is the only local evidence**: same-intent pairs that must hit, and similar-but-different pairs that must miss. The threshold is lowered incrementally against measured false-positive rate.
- **Once false positives no longer fall with a stricter threshold**, the embedding model rather than the threshold is the binding constraint, and a stronger embedder is the remaining lever.
- **Domains where small wording differences invert meaning** — clinical, legal, financial, where "under 18" versus "under 80" changes the answer — take a stricter threshold and accept a lower hit rate.

One recurring source of misconfiguration is polarity. Some libraries score **distance** (lower is closer, so the threshold is a ceiling) and others score **similarity** (higher is closer, so the threshold is a floor). Redis's `SemanticCache` exposes `distance_threshold`, set to `0.1` in the RedisVL user guide's example; **raising it widens the match band**, which is the opposite of the effect a similarity-valued knob would have.

## GPTCache

[GPTCache](https://github.com/zilliztech/gptcache), from Zilliz, is the reference open-source implementation, and its module boundaries correspond to the four stages: an **LLM adapter** (OpenAI, LangChain, llama.cpp and others), an **embedding generator**, a **vector store**, **cache storage**, and a **similarity evaluator**. The adapter wraps the OpenAI client so that existing call sites continue to invoke `openai.ChatCompletion.create` unchanged:

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

# Checks the cache first; reaches OpenAI only on a miss.
resp = openai.ChatCompletion.create(
    model="gpt-3.5-turbo",
    messages=[{"role": "user", "content": "How do I reset my password?"}],
)
```

`SearchDistanceEvaluation` scores the ANN result and `Config(similarity_threshold=...)` decides hit against miss. Substituting Milvus or Qdrant for `VectorBase("faiss", ...)`, or a different embedder for `Onnx()`, leaves the adapter and evaluator unchanged.

## Redis: RedisVL, vector sets, LangCache

RedisVL's `SemanticCache` is the self-hosted object: it takes a Redis URL and a vectorizer, and exposes `store(prompt, response)` and `check(prompt)`.

```python
from redisvl.extensions.cache.llm import SemanticCache
from redisvl.utils.vectorize import HFTextVectorizer

llmcache = SemanticCache(
    name="llmcache",
    redis_url="redis://localhost:6379",
    distance_threshold=0.1,                                   # ceiling, not floor
    vectorizer=HFTextVectorizer("redis/langcache-embed-v1"),
)
llmcache.set_ttl(3600)                                        # staleness control

if hit := llmcache.check(prompt="How do I change my password?"):
    answer = hit[0]["response"]
else:
    answer = call_llm(prompt)
    llmcache.store(prompt="How do I change my password?", response=answer)
```

`set_threshold()` retunes at runtime, `set_ttl()` governs expiry, and declaring `filterable_fields` (for example a `user_id` tag) allows `check` to take a `filter_expression`, so that one tenant's entry is not a candidate neighbour for another's query. The cache is backed by an ordinary Redis search index: the user guide's `rvl index info` output shows a hash index whose `prompt_vector` field is 768-dimensional `FLOAT32` with a `COSINE` distance metric. Redis separately ships **vector sets**, a similarity data type modelled on sorted sets that pairs each element with a vector instead of a score. **LangCache** is Redis's managed service over the same idea, in preview at the time of writing: `POST /v1/caches/{cacheId}/entries/search` embeds the prompt and searches, returning a cached response on a hit and an empty response on a miss, after which the application writes the fresh pair back with `POST /v1/caches/{cacheId}/entries`. Similarity thresholds, TTLs and eviction policies are configurable.

### Implementation sketch (Scala)

The load-bearing logic is the read-through decision, not the index. Both `Embedder` and `AnnIndex` below stand in for whichever backend is wired in.

```scala
trait Embedder:
  def embed(text: String): Vector[Float]

trait AnnIndex:
  /** Approximate nearest neighbour, with cosine distance in [0, 2]. */
  def nearest(q: Vector[Float], tenant: String): Option[(String, Double)]
  def insert(q: Vector[Float], tenant: String, response: String): Unit

final class SemanticCache(
    embedder: Embedder,
    index: AnnIndex,
    maxDistance: Double            // ceiling: smaller is stricter
):
  def completion(prompt: String, tenant: String)(call: String => String): String =
    val q = embedder.embed(prompt)
    // The tenant is part of the search, not a post-filter: a neighbour from
    // another tenant must never be a candidate at all.
    index.nearest(q, tenant) match
      case Some((cached, d)) if d <= maxDistance => cached
      case _ =>
        val fresh = call(prompt)
        index.insert(q, tenant, fresh)
        fresh
```

Two properties survive the abstraction. **Tenancy is a search parameter rather than a filter applied to results**, because an ANN index that returns top-1 across all tenants can return only a foreign entry and thus force a miss where a valid same-tenant entry existed. And **the miss path writes the pair back keyed by the vector of the phrasing that missed**, so the stored set accumulates one entry per distinct phrasing until eviction, not one per intent.

## Where it pays and where it does not

Semantic caching pays on **high-volume, repetitive, tolerant** traffic: support and FAQ bots, documentation question answering, retrieval-augmented generation front doors where many users ask a small set of questions in many phrasings. Portkey reports cost reductions of up to roughly 86% on such workloads; figures of that size come from vendor write-ups rather than independent benchmarks.

It is unsuitable or hazardous when:

- **Answers are personalised or stateful.** "What is my account balance?" must not match a neighbour's entry; scope by per-user filter or do not cache.
- **Freshness is part of correctness.** Prices, weather, inventory require short TTLs or no cache.
- **Small wording differences invert meaning.** Strict thresholds are required, which erodes the hit rate that motivated the cache.
- **Traffic is naturally unique.** Long code-generation or creative prompts rarely repeat, so embedding and search cost is paid for a hit rate near zero.

The threshold is a product decision rather than a constant: it selects a point between "cheap and occasionally wrong" and "correct and rarely cached", and only a domain validation set locates that point.

## Pitfalls

- **Threshold polarity inverted.** Treating `distance_threshold` as a similarity floor and raising it to "be stricter" widens the match band instead, and false hits increase sharply.
- **False hits are silent.** No exception, no log line, no latency anomaly distinguishes a wrong cached answer from a right one; without a validation set the error rate is unobserved.
- **Tenant scoping applied after retrieval.** Filtering foreign entries out of a top-1 ANN result converts a valid same-tenant hit into a miss; unfiltered, it leaks one user's answer to another.
- **Embedding model swapped in place.** Vectors written by the old embedder are not comparable to queries embedded by the new one, so distances become meaningless and the store must be rebuilt.
- **No TTL on volatile answers.** Price, inventory and weather responses persist indefinitely and are served long after they became wrong, with a rising hit rate masking the problem.
- **Tuning past the embedder's ceiling.** When false positives stop falling as the threshold tightens, further tightening only destroys the hit rate; the residual errors come from the embedding space, not the cut-off.
- **Caching before deduplicating.** Long, near-unique prompts pay embedding plus ANN search on every request for a hit rate near zero, adding latency to a path that never returns a hit.
