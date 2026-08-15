---
title: "LLM Model Routing: Dispatching Each Request to the Cheapest Adequate Model"
date: 2026-08-10
track: sys-patterns
summary: "Serving every request from one frontier model overpays for the easy majority of traffic. Model routing places a cheap decision — a learned win-rate predictor, an embedding-similarity match, or an optimistic cascade — in front of a pool of models of differing cost. This article covers RouteLLM's predictive routing and its published call-performance figures, semantic-router's embedding routing, cascade escalation, and the calibration and drift failure modes each one carries."
reading_time: 7
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

**Gist.** Large language model (LLM) traffic is long-tailed in difficulty: reformatting, classification and frequently-asked-question lookups sit alongside a minority of hard reasoning and code tasks, yet a single-model architecture charges frontier price and frontier latency for all of them. Model routing inserts a decision cheaper than either model — a learned strong-win predictor, a cosine-similarity match against labelled example utterances, or an optimistic cheap-first call with a verifier — and dispatches each prompt to the least expensive model expected to answer it adequately. The cost is a second calibrated component in the serving path: it has its own latency budget, its own accuracy, and a threshold whose correctness decays as the prompt distribution drifts.

## The routing decision as a serving-layer component

Routing is structurally the same move as an L7 (application-layer) load balancer or a cache tier: a cheap decision guards an expensive resource. The distinguishing constraint is the cost ratio. **The router's own latency and cost must remain a small fraction of a weak-model call**, because the router runs on every request while the strong model runs on only a fraction of them. A router that requires a generation step to decide has already spent an inference to save an inference.

Three families of routers differ in how much machinery they need and in what they require the operator to supply: a training signal, a taxonomy, or a verifier.

## Predictive routers: learned strong-win probability (RouteLLM)

RouteLLM, from LMSYS, reduces routing to a binary choice between one fixed **strong** model and one fixed **weak** model. The router is trained on human preference data from Chatbot Arena battles to predict, for a given prompt, the probability that the strong model's response would win the comparison. A **threshold** turns that score into a dispatch: prompts scoring above it go to the strong model, the remainder to the weak model. The threshold is the single knob that moves the system along the cost/quality curve.

The published measurements on the LMSYS blog are the reason the pattern drew attention. On MT Bench, a router trained with data augmented by an LLM judge reached **95% of GPT-4's performance while routing only 26% of queries to GPT-4**. Holding that same 95%-of-GPT-4 bar, LMSYS reports **cost reductions of over 85% on MT Bench, 45% on MMLU and 35% on GSM8K** against always calling GPT-4 — the saving available at a fixed quality bar differs by benchmark, so it is a property of the workload as much as of the router. Against commercial routing offerings LMSYS reported matching performance while being **over 40% cheaper**. These are benchmark figures on benchmark distributions; the transferable claim is the shape of the curve, not the specific percentages.

Four trained routers ship with the framework: `mf` (matrix factorization, the recommended one), `sw_ranking` (a similarity-weighted Elo calculation), `bert` (a BERT classifier) and `causal_llm` (an LLM-based classifier), alongside a `random` baseline. The software development kit (SDK) mirrors the OpenAI client surface:

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

The threshold is not a quality target and cannot be read as one. **It is a quantile of the router's score distribution over a specific corpus**, produced by calibration against a desired strong-call rate:

```
python -m routellm.calibrate_threshold --routers mf \
    --strong-model-pct 0.5 --config config.example.yaml
# -> "For 50.0% strong model calls for mf, threshold = 0.11593"
```

`router-mf-0.11593` therefore means "route approximately half of *this* corpus to the strong model". Lowering `--strong-model-pct` raises the threshold and shifts traffic to the weak model. Because the mapping from threshold to call rate is defined by the calibration corpus, **a threshold calibrated on one prompt distribution encodes a different call rate on another** — the reason calibration must run on representative traffic rather than on a default.

## Semantic routing: nearest labelled utterance (semantic-router)

Learned win-rate routers answer "hard or easy". Where the useful partition is known in advance — billing enquiries, coding tasks, off-topic input — `aurelio-labs/semantic-router` decides by similarity instead of by training. A **route** is a name plus a handful of example utterances. Those utterances are embedded once; at request time the incoming query is embedded and assigned to the route with the highest cosine similarity. **The decision contains no generation step**, so its cost is one embedding call plus a vector comparison.

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

Invoking the router returns the matched route, or `None` when no route clears the similarity threshold. **The `None` case is the fallback path and the primary operational lever**: raising the threshold sends more traffic to the default model, lowering it forces more prompts into a named route. Encoders are pluggable — `OpenAIEncoder`, `CohereEncoder`, `HuggingFaceEncoder` and `FastEmbedEncoder` are supported — and a local encoder keeps the routing decision off the network. Against RouteLLM the trade is legibility for maintenance: the routing rule is a readable list of utterances that can be debugged directly, but the taxonomy is owned by the operator and must be extended as new query shapes appear.

## Cascade escalation: optimistic execution with a verifier

The third family makes no prediction. The cheap model is called first, a verifier inspects its output, and the expensive model is called only if the verifier rejects. This is the routing analogue of speculative decoding: optimistic execution on the cheap path with fallback on the expensive one.

The verifier determines whether the cascade pays. It may be a mechanical check (the output parses as JSON, the emitted code compiles, the generated SQL executes), a self-reported confidence score, or a small LLM-as-judge pass. **The expected cost is `c_weak + c_verify + p_fail · c_strong`**, so the cascade wins when verification is cheap and the failure rate is low, and loses on both counts when the weak model fails often — every request then pays for two models plus the verifier. A verifier that accepts wrong answers converts a cost saving into a quality regression that the call-rate metric will not reveal. Cascades compose with predictive routing: route first to divert prompts predicted hard, then cascade inside the remaining bucket.

### Implementation sketch (Scala)

The escalation loop, with the verifier as the load-bearing parameter:

```scala
enum Tier:
  case Weak, Strong

case class Answer(text: String, tier: Tier)

trait Model:
  def complete(prompt: String): String

/** Accepts an answer or rejects it, forcing escalation. */
type Verifier = (String, String) => Boolean  // (prompt, output) => accepted

final class Cascade(weak: Model, strong: Model, verify: Verifier):

  def answer(prompt: String): Answer =
    val cheap = weak.complete(prompt)
    // The weak call is always paid for; only rejection adds the strong call.
    if verify(prompt, cheap) then Answer(cheap, Tier.Weak)
    else Answer(strong.complete(prompt), Tier.Strong)

/** Threshold routing: score once, dispatch, never call both. */
final class Predictive(weak: Model, strong: Model, score: String => Double, threshold: Double):

  def answer(prompt: String): Answer =
    if score(prompt) >= threshold then Answer(strong.complete(prompt), Tier.Strong)
    else Answer(weak.complete(prompt), Tier.Weak)
```

The structural difference is visible in the types: `Cascade` observes the weak output before deciding and can pay twice; `Predictive` decides from the prompt alone and pays once, at the price of deciding without evidence from the answer.

## Evaluating the knob

Every router exposes one continuous parameter — a threshold, a similarity cutoff, an escalation trigger — and the meaningful artefact is the curve it traces, not a single operating point. The metric RouteLLM uses is a **call-performance graph**: strong-model call fraction on the x-axis, task quality on the y-axis. Two reference lines bound it. Random routing traces a straight line between weak-model and strong-model quality and is the baseline any router must beat; the strong-model-only score is the ceiling. **A router earns its place by bowing above the random line** — reaching most of the ceiling at a small strong-call fraction.

Producing that curve requires a few hundred representative prompts, a scorer appropriate to the task (exact match, unit tests, or an LLM judge), a sweep of the threshold across its range, and a record of both the strong-call rate and the mean score at each setting. The knee — where the quality curve flattens — marks the setting beyond which additional strong-model spend buys close to nothing.

## Pitfalls

- **A threshold copied from documentation encodes an unknown call rate.** The threshold is a quantile of the router's score distribution over the calibration corpus, so transplanting `0.11593` onto different traffic yields neither 50% strong calls nor any predictable figure.
- **Prompt-distribution drift degrades quality silently.** A threshold calibrated on last quarter's traffic keeps returning a score above a fixed cutoff for a shifted population; nothing errors, and the symptom is a slow change in the strong-model call rate. Alarming on that rate and recalibrating on a rolling sample is what makes the drift observable.
- **Router overhead consumes the saving.** If the decision costs a meaningful fraction of a weak-model call in latency or tokens, per-request routing cost is multiplied by total traffic while the saving applies only to the diverted fraction.
- **A permissive verifier turns a cascade into a quality regression.** Cost metrics improve because escalation rarely fires, and the accepted-but-wrong weak answers appear nowhere in the call-rate dashboard.
- **A cascade over a weak model with a high failure rate costs more than no routing.** Expected cost `c_weak + c_verify + p_fail · c_strong` exceeds `c_strong` once `p_fail` approaches 1.
- **Semantic routes decay as query phrasing changes.** New query shapes fail to clear the similarity threshold, return `None`, and fall through to the default model — the fallback rate rises without any route reporting an error.
- **Benchmark call rates are not traffic call rates.** The 26% strong-call figure comes from MT Bench, and the reported saving at the same quality bar falls to 45% on MMLU and 35% on GSM8K; the achievable strong-call fraction is a property of the workload's difficulty distribution.
