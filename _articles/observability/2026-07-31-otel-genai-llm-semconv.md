---
title: "Instrumenting LLM calls with OpenTelemetry's gen_ai semantic conventions"
date: 2026-07-31
track: observability
summary: "A field guide to the OpenTelemetry GenAI semantic conventions as they stand in mid-2026: the gen_ai.* span attributes, the token-usage metric, the move from per-message events to structured message attributes, and why every one of them is still marked Development."
reading_time: 6
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

If you run an LLM anywhere near production you eventually want the same three numbers you want from any dependency: how long did the call take, did it error, and what did it cost. OpenTelemetry now has a standard vocabulary for exactly that — the `gen_ai.*` semantic conventions — and the shape of that vocabulary changed enough over the last year that most tutorials you'll find are already wrong. Here's the current picture.

## Where the spec actually lives now

The GenAI conventions have moved out of the main `open-telemetry/semantic-conventions` repo into a dedicated [`semantic-conventions-genai`](https://github.com/open-telemetry/semantic-conventions-genai) repository. The old attribute pages under `opentelemetry.io/docs/specs/semconv/gen-ai/` are now redirect stubs — if you're reading a page there, you're reading a tombstone. The current schema version is `1.42.0`.

The headline caveat: **none of it is Stable.** As of mid-July 2026 no GenAI span, event, metric, or attribute is marked Stable — every one is still `Development`. Treat these as names that can and do move, and pin your dashboards accordingly.

## The attributes worth emitting

A single LLM chat call is modelled as a `CLIENT` span. The span name is `{operation} {model}`, e.g. `chat gpt-4o-mini`. The load-bearing attributes:

- **`gen_ai.operation.name`** — one of `chat`, `text_completion`, `generate_content`, `embeddings`, `invoke_agent`, `execute_tool`, `create_agent`, `invoke_workflow`.
- **`gen_ai.provider.name`** — `openai`, `anthropic`, `aws.bedrock`, etc. This **replaced `gen_ai.system`**, which was deprecated in v1.37.0 (August 2025). Many frameworks still emit both, so query defensively.
- **`gen_ai.request.model`** — what you asked for (`gpt-4o-mini`).
- **`gen_ai.response.model`** — what actually served you (`gpt-4o-mini-2024-07-18`). These differ, and the difference matters for cost attribution.
- **`gen_ai.usage.input_tokens`** / **`gen_ai.usage.output_tokens`** — token counts. Older instrumentation emits `prompt_tokens` / `completion_tokens`; coalesce.
- **`gen_ai.response.finish_reasons`** — array like `["stop"]` or `["tool_calls"]`.

## Prompts and completions: no longer events

This is the change that trips people up. The convention used to record each message as a separate log/span **event** (`gen_ai.user.message`, `gen_ai.choice`, …). Since v1.37.0 that's gone. Message content is now carried as **structured span attributes**: `gen_ai.input.messages`, `gen_ai.output.messages`, and `gen_ai.system_instructions`.

These are opt-in and off by default — they contain raw prompt and completion text, i.e. PII and secrets. Turn them on deliberately (in the Python instrumentation, `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`), and keep them out of any backend you don't trust with user data.

## The one metric that pays the bills

For cost and capacity you want the metric, not the spans. The convention defines a histogram **`gen_ai.client.token.usage`**, dimensioned by **`gen_ai.token.type`** (`input` vs `output`), plus `gen_ai.provider.name` and `gen_ai.request.model`. Latency lives in the companion histogram `gen_ai.client.operation.duration`. A single PromQL-style query over `gen_ai.client.token.usage` split by model and token type gives you a spend dashboard without parsing a single span.

## A minimal hand-rolled span

If your framework isn't instrumented, emitting a conformant span by hand is a dozen lines:

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

## What to actually reach for

You rarely hand-roll this. Realistic options:

- **OpenTelemetry's own GenAI instrumentation** — packages like `opentelemetry-instrumentation-openai-v2` auto-emit the spans and metrics above.
- **OpenLLMetry** (Traceloop) — broad framework coverage, tracks the OTel conventions.
- **OpenInference** (Arize) — an adjacent convention that overlaps but is not byte-identical; check the attribute names if you mix.

And increasingly the agents emit natively: Claude Code, OpenAI Codex, and VS Code Copilot all ship OTel metrics/logs (Claude Code has beta trace support). Point them at your collector and you get token usage for free.

The pragmatic stance for mid-2026: adopt the conventions now — they're the only game in town and the field names are stable enough in practice — but build a thin normalization layer that coalesces the `system`/`provider.name` and `prompt`/`input` variants, because you will see multiple generations of attributes in the same trace.

**Try next:** Stand up an OpenTelemetry Collector locally, set `OTEL_EXPORTER_OTLP_ENDPOINT` for one OpenAI-backed script instrumented with `opentelemetry-instrumentation-openai-v2`, and confirm a `chat {model}` span plus a `gen_ai.client.token.usage` data point land in your backend split by `gen_ai.token.type`.
