---
title: "Guided decoding: masking logits so the model can only emit valid JSON"
date: 2026-08-03
track: sys-patterns
summary: "A model that must return JSON Schema shouldn't be trusted to; it should be unable to do otherwise. Guided decoding compiles the schema into an automaton and, at every decode step, sets the logits of any token that would break the structure to negative infinity. The engineering problem is doing that mask over a 128k-token vocabulary without slowing generation — solved by precomputing per-state token masks and overlapping mask construction with the GPU forward pass. This is how vLLM's structured outputs and XGrammar hit near-zero overhead."
reading_time: 5
tags: [llm, serving, structured-output, constrained-decoding, xgrammar, vllm]
sources:
  - title: "Willard & Louf — Efficient Guided Generation for Large Language Models (arXiv:2307.09702)"
    url: "https://arxiv.org/abs/2307.09702"
  - title: "Dong et al. — XGrammar: Flexible and Efficient Structured Generation Engine for LLMs (arXiv:2411.15100)"
    url: "https://arxiv.org/abs/2411.15100"
  - title: "MLC Blog — Achieving Efficient, Flexible, and Portable Structured Generation with XGrammar (2024-11-22)"
    url: "https://blog.mlc.ai/2024/11/22/achieving-efficient-flexible-portable-structured-generation-with-xgrammar"
  - title: "MLC Blog — XGrammar-2: Fast and Customizable Structured Generation for Tool Calling and Agents (2026-05-04)"
    url: "https://blog.mlc.ai/2026/05/04/xgrammar-2-fast-customizable-structured-generation"
  - title: "vLLM documentation — Structured Outputs"
    url: "https://docs.vllm.ai/en/latest/features/structured_outputs/"
---

Ask a model for JSON and prompt engineering gets you *mostly* JSON: a stray markdown fence, a trailing comma, a hallucinated field, an unterminated string when it hits the max-token limit. In a pipeline that parses the output, "mostly" is an outage waiting for the tail of the distribution. The retry-and-reparse loop that teams bolt on top is the wrong layer to fix it at. The right layer is the sampler.

Decoding is, at each step, a categorical draw over the vocabulary: the model emits a logit per token, you softmax and sample one. **Guided (constrained) decoding** inserts a step between the logits and the sample — it sets the logit of every token that would violate the target structure to `-inf`, so those tokens have exactly zero probability of being drawn. The model isn't *asked* to produce valid JSON; the tokens that would make it invalid are simply not on the menu. Output conformance stops being a probabilistic hope and becomes a structural guarantee.

## The finite-state intuition

Start with a regular constraint — say a phone number `\d{3}-\d{3}-\d{4}`. Compile the regex to a **DFA**. Each state knows which *characters* may come next; after `748` the automaton allows a digit, and once three more digits and a dash and four digits are in place it allows only end-of-string. A constraint that's a regular language is exactly a finite-state machine.

The complication is that LLMs don't emit characters, they emit **tokens** — multi-character chunks from a vocabulary of 100k–256k entries. So "which characters are legal next" has to become "which of my 128k token IDs keep the automaton in a valid state," and that question is asked *every single decode step*. Testing every token against the FSM at every step is O(vocab × token-length) — ruinous.

The move that made this practical is from Willard & Louf's Outlines paper (arXiv:2307.09702): **precompute an index once, before generation**, mapping each FSM state to the set of token IDs that are valid from it. At runtime each step is an O(1) lookup of an allowed-token set for the current state, turned into a boolean mask. The expensive automaton work happens at compile time; the hot path is a set membership test.

## Why JSON needs a stack, not just states

A regex FSM can't count. JSON is recursively nested — an object inside an array inside an object — and matching brackets to arbitrary depth is not a regular language; it's **context-free**. You need a grammar and a **pushdown automaton** (a state machine plus a stack) to track "I'm three levels deep, the last open delimiter was `[`, so `]` is legal but `}` is not."

[XGrammar](https://blog.mlc.ai/2024/11/22/achieving-efficient-flexible-portable-structured-generation-with-xgrammar) (mlc-ai, paper arXiv:2411.15100) is the engine that made CFG-constrained decoding fast enough to leave on in production. Its key observation: split the vocabulary into two classes. **Context-independent tokens** — whose validity depends only on the automaton's current position, not the stack contents — are typically *over 99%* of the vocabulary, and their masks can be precomputed into an **adaptive token mask cache**. Only the small remainder of **context-dependent tokens** need the stack inspected at runtime. Layer on a persistent-stack representation and, crucially, **overlapping mask generation on CPU with the model's forward pass on GPU**, and the masking cost largely disappears behind work you were doing anyway. The paper reports up to **3.5×** lower per-step masking overhead for JSON-schema (up to **10×** for full CFG), and end-to-end speedups up to **14×** / **80×** versus prior constrained-decoding stacks — approaching zero added latency in single-request serving.

The follow-up [XGrammar-2](https://blog.mlc.ai/2026/05/04/xgrammar-2-fast-customizable-structured-generation) (May 2026) pushes on grammar *compilation* rather than the decode step: up to **80× faster compilation** than XGrammar, a repetition-state compression that takes a large-array grammar from 534 ms to 5.37 ms, cross-grammar caching that reuses ~50% of structure across multiple tools, and a **Structural Tag** DSL aimed at tool-calling and agents (OpenAI Harmony channels, multi-model tool protocols) with batch and speculative-decoding-friendly APIs. Compilation speed matters because agent workloads swap grammars constantly — a new tool schema per turn — so amortizing the automaton build across requests is the current frontier.

## Using it: vLLM structured outputs

In vLLM this is exposed as **structured outputs**, with `StructuredOutputsParams` carrying one of `json`, `regex`, `choice`, `grammar` (EBNF), or `structural_tag`. The backend is selected by `structured_outputs_config` and defaults to `auto`, which picks among **xgrammar**, **guidance/llguidance**, and **outlines** based on the request. Over the OpenAI-compatible server you just pass a JSON Schema via `response_format`:

```python
from openai import OpenAI
from pydantic import BaseModel

class CarDescription(BaseModel):
    brand: str
    model: str
    year: int

client = OpenAI(base_url="http://localhost:8000/v1", api_key="-")
model = client.models.list().data[0].id

completion = client.chat.completions.create(
    model=model,
    messages=[{"role": "user", "content": "Generate an iconic 90s sports car."}],
    response_format={
        "type": "json_schema",
        "json_schema": {"name": "car", "schema": CarDescription.model_json_schema()},
    },
)
print(completion.choices[0].message.content)   # guaranteed to parse against the schema
```

Offline, the same guarantee comes from `SamplingParams`. A classifier that must return exactly one label is the cleanest case — a `choice` constraint means the model *cannot* emit anything but the listed strings:

```python
from vllm import LLM, SamplingParams
from vllm.sampling_params import StructuredOutputsParams

llm = LLM(model="HuggingFaceTB/SmolLM2-1.7B-Instruct")
params = SamplingParams(
    structured_outputs=StructuredOutputsParams(choice=["Positive", "Negative"])
)
print(llm.generate("Classify sentiment: vLLM is wonderful!", params)[0].outputs[0].text)
```

## What the guarantee does and doesn't buy

Constrained decoding guarantees the output is *well-formed* against the grammar. It does not guarantee it's *correct* — a schema-valid object can still contain a wrong value or a hallucinated-but-type-correct field, and over-tight grammars can distort the distribution or push the model into awkward token paths (mask away the natural continuation and you may degrade quality, not just format). It also interacts with tokenizer quirks: whitespace and token-boundary handling in the grammar compiler is where real bugs live. And the mask only enforces *your* schema — if the schema is under-specified, the model has room to be creatively unhelpful within it. Treat it as making malformed output *impossible* and semantic errors *still your problem*.

**Try next:** take one endpoint in your stack that parses model output and currently guards it with a try/except-and-retry, define its expected shape as a Pydantic model, and re-issue the call through vLLM with `response_format` json_schema (or an offline `StructuredOutputsParams(json=...)`). Then run a few hundred prompts and count parse failures — the number should go to zero — and separately eyeball value-level correctness, because that's the error class the mask can't touch.
