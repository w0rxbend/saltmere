---
title: "Tail Sampling: Keep the Traces That Actually Matter"
date: 2026-07-26
track: observability
summary: "Head sampling decides whether to keep a trace before it knows the outcome. Tail sampling waits for the whole trace, then keeps every error and every slow request — at the cost of buffering full traces in memory on one collector."
reading_time: 5
tags: [opentelemetry, tail-sampling, collector, tracing, observability, load-balancing]
sources:
  - title: "tailsamplingprocessor README (opentelemetry-collector-contrib)"
    url: "https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/tailsamplingprocessor/README.md"
  - title: "loadbalancingexporter README (opentelemetry-collector-contrib)"
    url: "https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/loadbalancingexporter/README.md"
  - title: "Tail Sampling with OpenTelemetry: Why it's useful, how to do it, and what to consider (OpenTelemetry blog)"
    url: "https://opentelemetry.io/blog/2022/tail-sampling/"
  - title: "Sampling (OpenTelemetry docs)"
    url: "https://opentelemetry.io/docs/concepts/sampling/"
  - title: "Tail-Based Sampling in OpenTelemetry: Sizing, Memory Crashes and Cost Model (Michal Drozd)"
    url: "https://www.michal-drozd.com/en/blog/otel-tail-sampling/"
---

Sampling exists because sending every span to your backend is expensive, and most of those spans look identical: a healthy request that took 40ms and returned 200. The question sampling has to answer is *which traces do you throw away*, and the honest problem with the simplest answer — decide randomly, decide early — is that it throws away exactly the traces you'd want if something went wrong.

## Head sampling: fast, but blind

Head sampling makes the keep-or-drop decision at the start of a trace, usually in the SDK, before the request has done anything. A common implementation is consistent probability sampling: hash the trace ID, keep it if the hash falls under a threshold. It's cheap, requires no coordination between services, and every participating service can independently arrive at the same decision for the same trace ID.

The cost is that the decision is made with zero information about how the trace turns out. A 1% sampling rate keeps 1% of your errors and 1% of your p99-blowing requests, same as it keeps 1% of everything else. If errors are rare — and they usually are — your sampled data can miss an incident entirely.

## Tail sampling: informed, but expensive

Tail sampling flips the order: let the trace finish, look at what actually happened (status code, duration, attributes), and *then* decide whether to keep it. This means you can write a policy like "keep 100% of errors, keep every trace slower than 2 seconds, and keep a random 10% of everything else" — which is a categorically better sampling strategy than anything head sampling can express, because it's conditioned on outcome.

The price is that a collector has to hold open, in memory, every span belonging to a trace until that trace is considered complete. Only then can it evaluate policies and either release the trace to the exporter or drop it.

| | Head sampling | Tail sampling |
|---|---|---|
| Decision point | Trace start (in the SDK) | Trace end (in the collector) |
| Sees outcome (errors, latency)? | No | Yes |
| Memory cost | Negligible | Full trace buffered per in-flight trace |
| Coordination needed | None (consistent hashing) | All spans of a trace must reach one collector |
| Good at | Uniform, cheap downsampling | Keeping errors and slow traces |
| Failure mode | Misses rare interesting traces | OOM / dropped-trace fallback under load |

## Why tail sampling needs one collector per trace

A trace's spans are usually emitted by many different services, each shipping to whatever collector instance is closest or least loaded. That's fine for head sampling, since the decision doesn't depend on the rest of the trace. It's fatal for tail sampling: if span A of a trace lands on collector-1 and span B lands on collector-2, neither collector ever sees the complete trace, so neither can make a correct sampling decision.

The fix is the **load-balancing exporter**. It sits in front of your tail-sampling collectors and routes spans by consistent hashing on `traceID`, so every span for a given trace always lands on the same downstream collector instance, regardless of which upstream service or agent sent it.

```yaml
# Layer 1: load balancer — routes all spans of a trace to the same backend
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317

exporters:
  loadbalancing:
    routing_key: "traceID"
    protocol:
      otlp:
        timeout: 1s
    resolver:
      dns:
        hostname: tail-sampling-collector-headless.observability.svc.cluster.local
        port: 4317

service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [loadbalancing]
```

The `dns` resolver (or `k8s`, or a static `hostnames` list) tells the load balancer which backend instances exist; `routing_key: traceID` is what guarantees the same trace always hashes to the same backend. Without that setting — or without the load-balancing layer at all — tail sampling behind more than one collector replica silently produces incomplete traces.

## The tail_sampling processor

On the backend tier, the `tail_sampling` processor buffers spans by trace ID, waits `decision_wait` for the trace to look complete, evaluates its `policies` in order, and forwards the trace if any policy says sample.

```yaml
# Layer 2: tail-sampling collector — one of the backends the LB routes to
processors:
  tail_sampling:
    decision_wait: 10s
    num_traces: 100000
    expected_new_traces_per_sec: 500
    policies:
      # keep every trace that contains an error
      - name: errors
        type: status_code
        status_code:
          status_codes: [ERROR]

      # keep every trace slower than 2s
      - name: slow-traces
        type: latency
        latency:
          threshold_ms: 2000

      # composite policy: sample errors on the checkout service at a
      # different rate than baseline, by combining two sub-policies
      - name: checkout-errors
        type: and
        and:
          and_sub_policy:
            - name: is-checkout
              type: string_attribute
              string_attribute:
                key: service.name
                values: [checkout]
            - name: is-error
              type: status_code
              status_code:
                status_codes: [ERROR]

      # everything else: keep a random 10% baseline
      - name: baseline
        type: probabilistic
        probabilistic:
          sampling_percentage: 10

exporters:
  otlp:
    endpoint: backend.example.com:4317

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [tail_sampling]
      exporters: [otlp]
```

Policies are evaluated independently and combined with OR — if any policy votes "sample," the trace is kept. The `and` type is how you build AND logic *within* one policy (e.g., "this service AND an error"), which is the building block for composite policies like the `checkout-errors` example above.

## Memory and cost: the part that bites in production

`num_traces` and `decision_wait` are not tuning knobs you can ignore — they set the shape of your memory footprint. Every trace held during its `decision_wait` window occupies real memory: rough sizing is `traces_per_second × decision_wait × avg_spans_per_trace × bytes_per_span`. At 1,000 traces/sec, a 10s `decision_wait`, 10 spans/trace, and ~1KB/span, that's roughly 100MB just for the buffer, before collector overhead.

The dangerous failure mode isn't a crash — it's silent data loss with the wrong priority. When `num_traces` is exceeded, the processor evicts the *oldest* buffered traces to make room for new ones, regardless of whether those old traces were about to be flagged as errors. Undersizing this buffer means you drop exactly the incident traces the whole point of tail sampling was to keep, and it tends to happen right when traffic (and error volume) spikes during an outage — the worst possible time.

Tail sampling also concentrates cost differently than head sampling: instead of a flat per-span SDK cost spread across every service, you're running a dedicated, memory-heavy collector tier that must scale with peak trace concurrency, not average throughput. It pays off once trace volume is high enough that indiscriminate collection is unaffordable, but the break-even point is a capacity-planning exercise, not a default you flip on.

**Try next:** Stand up a two-tier collector (load-balancing tier + tail_sampling tier) locally with the `dns` or `static` resolver, send it a mix of synthetic error and success traces, and watch the `otelcol_processor_tail_sampling_sampling_trace_dropped_too_early` metric to see what happens when you undersize `num_traces`.
