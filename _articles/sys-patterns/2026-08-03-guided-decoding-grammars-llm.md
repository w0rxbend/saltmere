---
title: "Guided decoding: masking logits so the model can only emit valid JSON"
date: 2026-08-03
track: sys-patterns
summary: "Prompting alone cannot guarantee that a large language model returns JavaScript Object Notation (JSON) conforming to a schema. Guided decoding compiles the schema into an automaton and, at every decode step, sets the logit of any token that would break the structure to negative infinity. The engineering problem is applying that mask over a vocabulary of 100k–256k tokens without slowing generation; XGrammar precomputes per-state token masks and overlaps mask construction with the GPU forward pass, and vLLM exposes the result as structured outputs."
reading_time: 6
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

**Gist.** A large language model (LLM) prompted to return JavaScript Object Notation (JSON) produces text that is usually parseable and occasionally not: a stray markdown fence, a trailing comma, an unterminated string when the maximum-token limit is reached. Guided decoding moves the constraint from the prompt to the sampler — a grammar is compiled into an automaton, and at each decode step the logit of every token that would leave the automaton in no valid state is set to `-inf`, giving those tokens zero sampling probability. The cost is a per-step mask over a vocabulary of 100k–256k entries, plus a grammar-compilation step before generation, plus a distribution that is no longer the model's own.

## The finite-state case

Decoding is a categorical draw over the vocabulary: the model emits one logit per token, the logits are softmaxed, one token is sampled. Guided (constrained) decoding inserts a step between the logits and the sample.

Take a regular constraint, a phone number `\d{3}-\d{3}-\d{4}`. The regular expression compiles to a **deterministic finite automaton (DFA)**, whose every state records which *characters* may follow. A constraint expressible as a regular language is exactly a finite-state machine, and character-level legality is a table lookup.

The mismatch is that an LLM emits **tokens**, not characters — multi-character chunks drawn from the vocabulary. The runtime question is therefore not "which characters are legal" but "which token identifiers keep the automaton in a valid state", and it is asked at **every decode step**. Advancing the automaton character by character through each candidate token costs on the order of vocabulary size times token length per step.

Willard & Louf (arXiv:2307.09702) remove that cost by **precomputing an index once, before generation**: a map from each automaton state to the set of token identifiers valid from it. At runtime a step is a lookup of the allowed-token set for the current state, converted to a boolean mask. **The automaton traversal happens at compile time; the hot path is a set-membership test.**

## Why JSON requires a stack

A finite-state machine cannot count. JSON nests recursively — an object inside an array inside an object — and matching delimiters to arbitrary depth is not a regular language but a **context-free** one. The recogniser is a **pushdown automaton**: a state machine plus a stack, so that at depth three with `[` as the innermost open delimiter, `]` is legal and `}` is not.

The invariant guided decoding maintains is that **the concatenation of emitted tokens is always a prefix of some string in the grammar's language**. A token is admissible only if consuming its characters leaves the pushdown automaton in a configuration from which some continuation completes. End-of-sequence is admissible only when the stack is empty and the current state is accepting — which is what prevents the truncated-string failure mode, at the price of a request that hits its token budget mid-structure returning a valid prefix rather than a valid document.

[XGrammar](https://blog.mlc.ai/2024/11/22/achieving-efficient-flexible-portable-structured-generation-with-xgrammar) (mlc-ai; paper arXiv:2411.15100) makes context-free-grammar-constrained decoding cheap enough to leave enabled in production. Its central split is by token class. **Context-independent tokens** — those whose validity depends only on the automaton's current position and not on the stack contents — are reported as **over 99%** of the vocabulary, and their masks are precomputed into an **adaptive token mask cache**. Only the remaining **context-dependent tokens** require the stack to be inspected at runtime. Combined with a persistent-stack representation and with **mask generation on the CPU overlapped with the model's forward pass on the GPU**, the masking work is hidden behind computation already in flight. XGrammar's authors report up to **3.5×** lower logit-masking overhead for JSON Schema and up to **10×** for full context-free grammars, with end-to-end speedups up to **14×** and **80×** respectively against existing engines, and describe the result as near-zero-overhead structured generation.

[XGrammar-2](https://blog.mlc.ai/2026/05/04/xgrammar-2-fast-customizable-structured-generation) (May 2026) targets grammar *compilation* rather than the decode step: up to **80× faster compilation** than XGrammar when scaling from 10 to 500 tools, a repetition compression that reduces one complex JSON Schema from **534 ms to 5.37 ms**, cross-grammar caching that reuses close to **50%** of structure when compiling a 50-tool schema set, and **Structural Tag**, a JSON-based domain-specific language for tool calling and agents (OpenAI Harmony channels, multi-model tool protocols) with batch and speculative-decoding-friendly application programming interfaces. Compilation cost is load-bearing for agent workloads, which supply a different tool schema per turn and therefore cannot amortise one automaton build over many requests.

## Interface: vLLM structured outputs

vLLM exposes the mechanism as **structured outputs**. `StructuredOutputsParams` carries one of `json`, `regex`, `choice`, `grammar` (Extended Backus–Naur Form), or `structural_tag`. The backend is set by `--structured-outputs-config.backend` and defaults to `auto`, documented as attempting to select an appropriate backend from the details of each request; the named alternatives include **xgrammar**, **guidance** (llguidance) and **outlines**. Over the OpenAI-compatible server the schema travels in `response_format`:

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
print(completion.choices[0].message.content)
```

Offline, the same guarantee comes from `SamplingParams`. A classifier constrained to one label is the narrowest case: a `choice` constraint admits no string outside the list.

```python
from vllm import LLM, SamplingParams
from vllm.sampling_params import StructuredOutputsParams

llm = LLM(model="HuggingFaceTB/SmolLM2-1.7B-Instruct")
params = SamplingParams(
    structured_outputs=StructuredOutputsParams(choice=["Positive", "Negative"])
)
print(llm.generate("Classify sentiment: vLLM is wonderful!", params)[0].outputs[0].text)
```

### Implementation sketch (Scala)

The load-bearing structure is the per-state cache plus the mask application; the model call and the grammar compiler are elided.

```scala
type TokenId = Int

/** A compiled grammar exposes: the state reached by consuming a token
  * (None if the token is inadmissible), and whether a state accepts. */
trait Automaton:
  def step(state: Int, token: TokenId): Option[Int]
  def accepting(state: Int): Boolean
  def contextIndependent(state: Int): Boolean

final class MaskCache(vocab: Vector[TokenId], aut: Automaton):
  // Populated once per grammar: state -> admissible token set.
  private val cache = collection.mutable.HashMap.empty[Int, Set[TokenId]]

  def allowed(state: Int): Set[TokenId] =
    if aut.contextIndependent(state) then
      cache.getOrElseUpdate(state, compute(state))
    else compute(state)                      // stack-dependent: no reuse

  private def compute(state: Int): Set[TokenId] =
    vocab.iterator.filter(t => aut.step(state, t).isDefined).toSet

def maskLogits(logits: Array[Float], allowed: Set[TokenId]): Array[Float] =
  logits.zipWithIndex.map: (z, i) =>
    if allowed.contains(i) then z else Float.NegativeInfinity

// One decode step: mask, sample, advance. End-of-sequence is admitted only
// from an accepting state, so the automaton gates termination.
def stepOnce(state: Int, logits: Array[Float], eos: TokenId, cache: MaskCache,
             sample: Array[Float] => TokenId, aut: Automaton): (TokenId, Int) =
  val base = cache.allowed(state)
  val tok  = sample(maskLogits(logits, if aut.accepting(state) then base + eos else base))
  if tok == eos then (tok, state)
  else (tok, aut.step(state, tok).getOrElse(sys.error("mask admitted an invalid token")))
```

The failure branch is not decoration: if masking and stepping disagree, the automaton has been advanced by a token its own mask should have excluded, which means the index and the runtime traversal have diverged.

## What the guarantee covers

Constrained decoding guarantees the output is **well-formed** against the grammar. It does not guarantee the output is **correct**: a schema-valid object may carry a wrong value, or a field that is type-correct and fabricated. Masking also changes the sampled distribution — the highest-probability continuation may be excluded, so the model is pushed onto token paths it did not favour. Where the schema is under-specified, the mask constrains nothing beyond the shape.

## Pitfalls

- **Truncation returns a valid prefix, not a valid document.** A request that reaches its maximum-token limit mid-object ends with a non-empty stack; the text parses as nothing, because end-of-sequence was never admissible and generation stopped anyway.
- **Grammar compilation is on the request path for agent workloads.** A new tool schema per turn means a fresh automaton build per turn; XGrammar-2's compilation work and cross-grammar caching exist against exactly this pattern, and without them the build dominates short generations.
- **Tokenizer boundaries do not align with grammar boundaries.** A single token may span a delimiter and the whitespace around it, so whitespace and token-boundary handling in the grammar compiler is where defects concentrate rather than in the automaton itself.
- **An over-tight grammar degrades content, not only format.** Masking away the natural continuation forces alternative token paths, so quality regressions appear as odd phrasing inside structurally perfect output.
- **Context-dependent tokens defeat the mask cache.** The reported **over 99%** context-independent share is a property of typical grammars; a grammar whose validity depends heavily on stack contents falls back to per-step traversal and loses the cached fast path.
- **`auto` backend selection is not a fixed backend.** vLLM's default chooses per request from the details of that request, so grammar-dialect differences between the available backends can surface as behaviour that varies with the constraint type used.
