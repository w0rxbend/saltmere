---
title: "Instrumenting LLM calls with OpenTelemetry's gen_ai semantic conventions"
date: 2026-07-31
track: observability
summary: "A survey of the OpenTelemetry GenAI semantic conventions as they stand in mid-2026: the gen_ai.* span attributes, the token-usage metric, the move from per-message events to structured message attributes, and the consequences of every one of them still being marked Development."
reading_time: 7
tags: [opentelemetry, observability, genai, llm, tracing, semconv, instrumentation]
sources:
  - title: "OpenTelemetry GenAI semantic conventions repository"
    url: "https://github.com/open-telemetry/semantic-conventions-genai"
  - title: "Inside the LLM Call: GenAI Observability with OpenTelemetry (OTel blog, May 2026)"
    url: "https://opentelemetry.io/blog/2026/genai-observability/"
  - title: "The state of the OpenTelemetry GenAI semantic conventions (July 2026) — John Hodge"
    url: "https://john-hodge.com/blog/opentelemetry-genai-semantic-conventions/"
  - title: "How OpenTelemetry Traces LLM Calls, Agent Reasoning, and MCP Tools — Greptime"
    url: "https://greptime.com/blogs/2026-05-09-opentelemetry-genai-semantic-conventions"
  - title: "Gen AI attribute registry — OpenTelemetry docs"
    url: "https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/"
---

**Gist.** A large language model (LLM) call is a remote dependency, and the operational questions asked of it are the usual three — latency, error rate, cost — but the cost dimension is denominated in tokens rather than requests, so generic HTTP instrumentation cannot answer it. OpenTelemetry's `gen_ai.*` semantic conventions supply a shared vocabulary for that: a `CLIENT` span per model call carrying request and response model identity plus token counts, and a token-usage histogram carrying the same identity as dimensions. The cost is instability: as of mid-2026 **no GenAI span, event, metric or attribute is marked Stable** — every one is `Development` — so attribute names have already moved once and any query written against them is a query against a moving target.

## Where the specification lives

The GenAI conventions have moved out of the main `open-telemetry/semantic-conventions` repository into a dedicated [`semantic-conventions-genai`](https://github.com/open-telemetry/semantic-conventions-genai) repository. The older attribute pages under `opentelemetry.io/docs/specs/semconv/gen-ai/` are redirect stubs; a page served from that path describes a superseded state of the specification.

The `Development` maturity level is the load-bearing fact for anyone building on top. It carries no compatibility promise across schema versions, which means a dashboard keyed on an attribute name is coupled to the version of the instrumentation library that emitted it, not to the specification.

## The span

A single chat call is modelled as a span of kind `CLIENT`. The span name is `{operation} {model}` — for example, `chat gpt-4o-mini` — which makes the name low-cardinality per model rather than per request. The attributes that carry the analysis:

- **`gen_ai.operation.name`** — values such as `chat`, `text_completion`, `generate_content`, `embeddings`, `create_agent`, `invoke_agent` and `execute_tool`.
- **`gen_ai.provider.name`** — `openai`, `anthropic`, `aws.bedrock`, and similar. This **replaced `gen_ai.system`, which the registry now marks deprecated**. Instrumentation in the field still emits both, sometimes within a single trace.
- **`gen_ai.request.model`** — the model identifier submitted (`gpt-4o-mini`).
- **`gen_ai.response.model`** — the model identifier the provider reports having served (`gpt-4o-mini-2024-07-18`). **These two differ whenever an alias resolves to a dated snapshot**, and since price lists are published per snapshot, cost attribution that keys on the request model attributes spend to the alias rather than to the model that was billed.
- **`gen_ai.usage.input_tokens`** and **`gen_ai.usage.output_tokens`** — token counts. Older instrumentation emits `prompt_tokens` and `completion_tokens` for the same quantities.
- **`gen_ai.response.finish_reasons`** — an array, such as `["stop"]` or `["tool_calls"]`, so a response carrying several choices reports one entry per choice.

## Prompts and completions moved from events to attributes

The convention previously recorded each message as a separate log or span **event**: `gen_ai.user.message`, `gen_ai.choice`, and siblings. **Since v1.37.0 those events are gone.** Message content is carried instead as structured span attributes: `gen_ai.input.messages`, `gen_ai.output.messages`, and `gen_ai.system_instructions`.

The migration is not a rename, and that is the failure mode: a backend query that selected span events by name returns nothing after an instrumentation upgrade, with no error and no dropped-data signal, because the data is present under a different shape.

These attributes are **opt-in and off by default**. They contain raw prompt and completion text, which is to say whatever personally identifiable information (PII) or credential material a user typed. In the Python instrumentation the switch is the environment variable `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`. Enabling it exports that text to every downstream processor and backend in the pipeline.

## The metric

Spans answer per-request questions; aggregate cost and capacity questions are answered by the metric, which does not require sampling decisions to be reconciled. The convention defines a histogram **`gen_ai.client.token.usage`**, dimensioned by **`gen_ai.token.type`** (`input` or `output`) together with `gen_ai.provider.name` and `gen_ai.request.model`. Latency lives in the companion histogram **`gen_ai.client.operation.duration`**.

The separation matters under sampling. Traces are commonly sampled at a fraction of traffic; metrics are aggregated over every recorded call. **A spend figure derived from sampled spans is scaled by the sampling ratio and is wrong by that factor unless the ratio is known and applied**; the same figure summed from the histogram is not.

### Implementation sketch (Scala)

Using the OpenTelemetry Java application programming interface (API) from Scala 3. The point is the attribute set and the double recording — one histogram observation per token type — not the client call itself.

```scala
val tracer: Tracer = openTelemetry.getTracer("my.llm.client")
val tokens: LongHistogram = openTelemetry.getMeter("my.llm.client")
  .histogramBuilder("gen_ai.client.token.usage").ofLongs().setUnit("{token}").build()

val Provider  = AttributeKey.stringKey("gen_ai.provider.name")
val ReqModel  = AttributeKey.stringKey("gen_ai.request.model")
val TokenType = AttributeKey.stringKey("gen_ai.token.type")

def chat(model: String, messages: List[Message]): Response =
  val span = tracer.spanBuilder(s"chat $model").setSpanKind(SpanKind.CLIENT).startSpan()
  val scope = span.makeCurrent()
  try
    span.setAttribute("gen_ai.operation.name", "chat")
    span.setAttribute(Provider, "openai")
    span.setAttribute(ReqModel, model)

    val resp = client.complete(model, messages)

    // response model is the snapshot actually served; it may differ from `model`
    span.setAttribute("gen_ai.response.model", resp.model)
    span.setAttribute("gen_ai.usage.input_tokens", resp.usage.inputTokens)
    span.setAttribute("gen_ai.usage.output_tokens", resp.usage.outputTokens)

    val base = Attributes.of(Provider, "openai", ReqModel, model)
    tokens.record(resp.usage.inputTokens,
      base.toBuilder.put(TokenType, "input").build())
    tokens.record(resp.usage.outputTokens,
      base.toBuilder.put(TokenType, "output").build())
    resp
  catch case e: Throwable => span.recordException(e); span.setStatus(StatusCode.ERROR); throw e
  finally { scope.close(); span.end() }
```

The equivalent in Python, where most published GenAI instrumentation lives:

```python
from opentelemetry import trace, metrics

tracer = trace.get_tracer("my.llm.client")
meter = metrics.get_meter("my.llm.client")
tokens = meter.create_histogram("gen_ai.client.token.usage", unit="{token}")

model = "gpt-4o-mini"
with tracer.start_as_current_span(f"chat {model}", kind=trace.SpanKind.CLIENT) as span:
    span.set_attribute("gen_ai.operation.name", "chat")
    span.set_attribute("gen_ai.provider.name", "openai")
    span.set_attribute("gen_ai.request.model", model)

    resp = client.chat.completions.create(model=model, messages=messages)

    u = resp.usage
    span.set_attribute("gen_ai.response.model", resp.model)
    span.set_attribute("gen_ai.usage.input_tokens", u.prompt_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", u.completion_tokens)
    span.set_attribute("gen_ai.response.finish_reasons",
                       [resp.choices[0].finish_reason])

    base = {"gen_ai.provider.name": "openai", "gen_ai.request.model": model}
    tokens.record(u.prompt_tokens, {**base, "gen_ai.token.type": "input"})
    tokens.record(u.completion_tokens, {**base, "gen_ai.token.type": "output"})
```

## Existing instrumentation

Hand-rolling is rarely necessary. The options in circulation:

- **OpenTelemetry's own GenAI instrumentation** — packages such as `opentelemetry-instrumentation-openai-v2` emit the spans and metrics above automatically.
- **OpenLLMetry** (Traceloop) — broad framework coverage, tracking the OTel conventions.
- **OpenInference** (Arize) — an adjacent convention that overlaps the OTel names without being identical to them. Mixing the two in one backend produces two attribute vocabularies describing the same call.

Agent tooling increasingly emits telemetry natively: Claude Code, OpenAI Codex and VS Code Copilot ship OTel metrics and event logs. Directing them at a collector yields token usage without application changes.

The consequence for adoption in mid-2026: the conventions are the only shared vocabulary available, and the practical requirement is a normalization layer that coalesces the `gen_ai.system` / `gen_ai.provider.name` and `prompt_tokens` / `input_tokens` variants at ingest, because multiple attribute generations will appear within a single trace whenever services upgrade instrumentation independently.

## Pitfalls

- **Cost attributed to `gen_ai.request.model`.** An alias resolves to a dated snapshot, so spend aggregates under the alias name and every snapshot's cost collapses into one series; `gen_ai.response.model` carries the served identity.
- **Queries selecting `gen_ai.user.message` or `gen_ai.choice` events.** Removed in v1.37.0 in favour of `gen_ai.input.messages` and `gen_ai.output.messages`; the query returns empty rather than failing, so the dashboard shows zero rather than an error.
- **Queries filtering on `gen_ai.system`.** Deprecated in favour of `gen_ai.provider.name`; services on mixed instrumentation versions emit one, the other, or both, so a filter on either name silently drops part of the traffic.
- **Spend computed from spans under head sampling.** Token sums scale with the sampling ratio; the `gen_ai.client.token.usage` histogram aggregates every recorded call and does not.
- **`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` in a shared pipeline.** Raw prompt and completion text becomes a span attribute and reaches every downstream processor and backend, including any with a retention policy or access model unsuited to user data.
- **Dashboards pinned to no schema version.** Every GenAI attribute is `Development`, so an instrumentation-library upgrade can rename a field with no compatibility shim, and the panel goes blank at deploy time rather than at query time.
- **Mixing OpenInference and OTel GenAI attributes.** The two conventions overlap but do not agree name-for-name, so a query written against one vocabulary sees only the subset of calls instrumented by that side.
