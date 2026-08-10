---
title: "LLM Model Routing: Send Each Request to the Cheapest Model That Can Handle It"
date: 2026-08-10
track: sys-patterns
summary: "Running one frontier model for every request is expensive and slow when most queries are easy. Model routing puts a lightweight classifier in front of a model pool that sends hard prompts to a strong model and easy ones to a cheap model. This walks through predictive routers (RouteLLM), semantic routing by embedding similarity (semantic-router), and cascade escalation, with runnable code and the published cost numbers."
reading_time: 6
tags: [llm-serving, model-routing, semantic-router, routellm, cost-optimization]
sources:
  - title: "RouteLLM: An Open-Source Framework for Cost-Effective LLM Routing (LMSYS Org)"
    url: "https://www.lmsys.org/blog/2024-07-01-routellm/"
  - title: "lm-sys/RouteLLM — README (routers, calibration)"
    url: "https://github.com/lm-sys/RouteLLM/blob/main/README.md"
  - title: "lm-sys/RouteLLM — Python SDK example"
    url: "https://github.com/lm-sys/RouteLLM/blob/main/examples/python_sdk.md"
  - title: "aurelio-labs/semantic-router — GitHub"
    url: "https://github.com/aurelio-labs/semantic-router"
  - title: "Semantic Router — Concepts Overview (Aurelio AI docs)"
    url: "https://docs.aurelio.ai/semantic-router/user-guide/concepts/overview"
---

## The problem: one big model for everything

The default architecture for an LLM feature is a single call to the best model you can afford. It is simple and it works, but it is wasteful. Traffic in most production systems is long-tailed: a large fraction of requests are "reformat this", "classify this ticket", "answer this FAQ" — queries a small model handles perfectly — and a minority are genuinely hard reasoning or code tasks that need a frontier model. Paying frontier prices, and eating frontier latency, on every request means overpaying for the easy majority.

Model routing is the serving-layer answer. It is the same idea as an L7 load balancer or a cache tier: put a cheap decision in front of an expensive resource. Here the cheap decision is *which* model, not *which* replica. A lightweight router looks at each incoming prompt, estimates how hard it is, and dispatches to a small/cheap model or a large/expensive one. The router itself must be far cheaper than the models it routes between, or the whole thing is pointless.

There are three main approaches worth knowing, in rough order of how much machinery they need.

## Approach A: predictive / learned routers (RouteLLM)

RouteLLM, from LMSYS, frames routing as a binary decision between a fixed **strong** model and a fixed **weak** model. It trains a router on human preference data (Chatbot Arena battles) to predict, for a given prompt, the probability that the strong model's answer would actually win. You then pick a **threshold**: prompts whose predicted "strong-win" score clears the threshold go to the strong model; everything else goes to the weak one. Slide the threshold and you slide along the cost/quality curve.

The published results are the reason this pattern got attention. On MT Bench, RouteLLM's matrix-factorization router reached **95% of GPT-4's performance while calling GPT-4 for only 26% of queries** (dropping to 14% of calls after augmenting the training data with an LLM judge), which the LMSYS post reports as **cost reductions of over 85%** versus always calling the strong model. The same "95% of GPT-4 performance" bar cost roughly **45% of calls on MMLU and 35% on GSM8K**. Against commercial routing offerings, LMSYS reported matching performance while being **over 40% cheaper**. Treat these as benchmark figures, not a promise for your traffic — but the shape (most queries don't need the big model) generalizes.

RouteLLM ships four trained routers: `mf` (matrix factorization, the recommended one), `sw_ranking` (a similarity-weighted Elo calculation), `bert` (a BERT classifier), and `causal_llm` (an LLM-based classifier), plus a `random` baseline. The SDK mimics the OpenAI client:

```python
from routellm.controller import Controller

client = Controller(
    routers=["mf"],
    strong_model="gpt-4-1106-preview",
    weak_model="anyscale/mistralai/Mixtral-8x7B-Instruct-v0.1",
    config={"mf": {"checkpoint_path": "routellm/mf_gpt4_augmented"}},
)

# Model string encodes the router and its threshold: router-{name}-{threshold}
response = client.chat.completions.create(
    model="router-mf-0.11593",
    messages=[{"role": "user", "content": "What's the square root of 144?"}],
)
print(response.choices[0]["message"]["content"])
```

Where does `0.11593` come from? You calibrate it against a target rate of strong-model calls, using your own query distribution:

```
python -m routellm.calibrate_threshold --routers mf \
    --strong-model-pct 0.5 --config config.example.yaml
# -> "For 50.0% strong model calls for mf, threshold = 0.11593"
```

So `router-mf-0.11593` means "route about half of *this* traffic to the strong model." Push `--strong-model-pct` down and the threshold rises, sending more to the weak model and cutting cost — at some point quality drops off, which is why you calibrate on representative prompts rather than guessing.

## Approach B: semantic routing by embedding similarity (semantic-router)

Learned win-rate routers are great when the axis you care about is "hard vs easy". But often you know your *categories* up front: billing questions go to a cheap model with a billing tool, coding questions go to a strong code model, off-topic chit-chat gets a canned deflection. For that, `aurelio-labs/semantic-router` is a much lighter tool. Instead of training a classifier, you define **routes** as a handful of example utterances, embed them once, and at request time embed the incoming query and pick the route with the highest cosine similarity. There is no generation step in the decision — it is a vector comparison, so it adds milliseconds, not a model call.

```python
from semantic_router import Route
from semantic_router.encoders import OpenAIEncoder
from semantic_router.routers import SemanticRouter

coding = Route(
    name="coding",
    utterances=[
        "write a python function to reverse a list",
        "why is my recursion throwing a stack overflow",
        "refactor this SQL query for performance",
    ],
)

simple_faq = Route(
    name="simple_faq",
    utterances=[
        "what are your business hours",
        "how do I reset my password",
        "where do I find my invoice",
    ],
)

encoder = OpenAIEncoder()
router = SemanticRouter(
    encoder=encoder,
    routes=[coding, simple_faq],
    auto_sync="local",
)

choice = router("my for-loop is off by one, help").name  # -> "coding"
model = "gpt-4o" if choice == "coding" else "gpt-4o-mini"
```

Calling the router returns the matched route (or `None` when nothing clears the similarity threshold, which is your cue to fall back to a default model). You then map route names to models, system prompts, and tools. The encoder is pluggable — `OpenAIEncoder`, `CohereEncoder`, `HuggingFaceEncoder`, and `FastEmbedEncoder` are all supported, and a local embedding model keeps the routing decision both cheap and private. The tradeoff versus RouteLLM: you get transparent, editable routing you can debug by reading the utterance lists, but you own the taxonomy and must add utterances as new query shapes appear. The `None` return is a real operational lever — tune the score threshold to control how aggressively you fall back.

## Approach C: cascade / speculative escalation

The third approach skips prediction entirely: **try the cheap model first, and escalate only if the answer is not good enough.** Send the prompt to the small model, run a fast verifier on its output, and re-issue to the large model only when the verifier rejects it. This is the routing analogue of speculative decoding — optimistic execution on the cheap path, fallback on the expensive one.

The verifier is the whole game. It can be a rule (did the output parse as valid JSON? did the code compile? did the SQL execute?), a self-reported confidence score, or a small LLM-as-judge check. Cascades shine when verification is cheap and reliable and when most easy queries pass on the first try, because the small-model cost is nearly free and only the hard tail pays for two calls. They hurt when the small model fails *often* (you pay for both models plus the verifier on most requests) or when a wrong small-model answer is expensive to catch. Cascades and predictive routing compose: route first to skip obviously-hard prompts, then cascade within the "probably easy" bucket.

## The cost/quality tradeoff, and how to evaluate

Every router exposes one knob — a threshold, a similarity cutoff, an escalation trigger — that trades quality for cost. Evaluating it means plotting the curve, not picking one number. The clean way, and the metric RouteLLM itself uses, is a **call-performance graph**: on the x-axis, the fraction of requests sent to the strong model; on the y-axis, task quality. Two reference points anchor it — random routing (a straight line between the weak and strong models) is the baseline any real router must beat, and the strong-model-only ceiling. A good router bows above the random line: it reaches most of the quality ceiling with a small fraction of strong-model calls.

To run this yourself: assemble a few hundred representative prompts with a scorer (exact match, unit tests, an LLM judge — whatever fits your task), sweep the router's threshold across its range, and at each setting record both the strong-model call rate and the average score. Read off the point where the quality curve flattens — that is your knee, the setting past which extra strong-model spend buys almost nothing. Two things that bite in production: **routing overhead** (the router's own latency and cost must stay a small fraction of the weak-model call, or it eats the savings) and **distribution drift** (a threshold calibrated on last quarter's traffic silently degrades as prompts change, so recalibrate on a rolling sample and alarm on the strong-model call rate). Route on the input, log the outcome, and let the logs recalibrate the threshold.

**Try next:** Take one endpoint, define three or four semantic-router routes over your real logs, and map two of them to a cheaper model behind a feature flag. Measure the strong-model call rate and quality delta for a week before touching the threshold.
