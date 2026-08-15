---
title: "Tail Sampling: Keeping the Traces That Carry Information"
date: 2026-07-26
track: observability
summary: "Head sampling decides whether to keep a trace before the outcome is known. Tail sampling waits for the whole trace, then keeps every error and every slow request — at the cost of buffering full traces in memory on one collector."
reading_time: 6
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

**Gist.** Storing every span is expensive, and a sampler that decides at trace start has no information about how the trace ends, so a 1% rate retains 1% of errors. Tail sampling in the OpenTelemetry Collector defers the decision until the trace is judged complete, evaluates policies against the observed status codes, latencies and attributes, and keeps the traces that carry information. The cost is a stateful collector tier that buffers every in-flight trace in memory and requires all spans of a trace to arrive at the same instance.

## Head sampling: a decision taken without evidence

Head sampling makes the keep-or-drop decision at the start of a trace, typically in the software development kit (SDK), before the request has executed. A common implementation is consistent probability sampling: hash the trace identifier and keep the trace if the hash falls below a threshold. **The decision is a pure function of the trace ID**, so every service touching that trace reaches the same verdict independently, with no coordination and no state.

The property that makes it cheap is the property that makes it blind. The sampler observes nothing about status codes or duration, so retention is uniform across outcomes: a 1% rate keeps 1% of errors and 1% of the requests in the latency tail, in the same proportion as everything else. When errors are rare relative to successes, a sampled dataset can contain no trace at all from an incident.

## Tail sampling: a decision conditioned on outcome

Tail sampling inverts the order. The trace is allowed to finish, the recorded outcome is inspected, and only then is the keep-or-drop decision taken. This admits policies that head sampling cannot express — retain all traces containing an error, retain all traces exceeding a latency threshold, retain a probabilistic baseline of the remainder — **because the predicate is evaluated over the completed trace rather than over its identifier**.

The obligation this creates is state. A collector must hold every span belonging to a trace in memory until the trace is considered complete; only then can policies be evaluated and the trace released to the exporter or discarded.

| | Head sampling | Tail sampling |
|---|---|---|
| Decision point | Trace start (in the SDK) | Trace end (in the collector) |
| Sees outcome (errors, latency)? | No | Yes |
| Memory cost | Negligible | Full trace buffered per in-flight trace |
| Coordination needed | None (consistent hashing) | All spans of a trace must reach one collector |
| Suited to | Uniform, cheap downsampling | Retaining errors and slow traces |
| Failure mode | Misses rare interesting traces | Eviction of buffered traces under load |

## The single-collector invariant

The spans of one trace are emitted by many services, each shipping to whichever collector instance its own configuration names. That distribution is harmless under head sampling, where no decision depends on the rest of the trace. Under tail sampling it breaks the mechanism: **if span A lands on collector-1 and span B on collector-2, neither instance ever holds the complete trace, and each evaluates its policies against a fragment.** A latency policy on a partial trace measures the wrong duration; a status-code policy misses an error recorded in the half of the trace it never received.

The `loadbalancingexporter` restores the invariant. Placed in a stateless first tier, it routes spans downstream by consistent hashing over a routing key, so that **all spans sharing a trace ID resolve to the same backend instance regardless of which agent submitted them**.

```yaml
# Tier 1: stateless router — all spans of a trace reach the same backend
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

The resolver — `dns` here, alternatively `k8s` or a static `hostnames` list — supplies the membership set over which the hash is taken. `routing_key: traceID` is the setting that binds a trace to one backend. Without the routing tier, or with a routing key that is not the trace ID, a deployment of more than one tail-sampling replica produces fragmented traces and no error is raised: the pipeline reports success while sampling on incomplete input.

Membership changes are worth noting: the resolver refreshes its host list, and a change in the backend set redistributes hash ranges. Traces in flight across such a change may have their remaining spans routed to a different instance than their earlier spans.

## The tail_sampling processor state machine

On the stateful tier, `tail_sampling` maintains a map from trace ID to buffered spans. A span for an unknown trace ID creates an entry and starts a timer; further spans append to it. **When `decision_wait` elapses since the first span of that trace, the trace is treated as complete** — the processor has no end-of-trace signal, so this timeout is the completeness heuristic. Policies are then evaluated and the trace is forwarded or dropped. `num_traces` bounds how many traces may be resident at once.

```yaml
# Tier 2: stateful sampler — one of the backends the router resolves to
processors:
  tail_sampling:
    decision_wait: 10s
    num_traces: 100000
    expected_new_traces_per_sec: 500
    policies:
      # retain every trace containing an error
      - name: errors
        type: status_code
        status_code:
          status_codes: [ERROR]

      # retain every trace exceeding 2s
      - name: slow-traces
        type: latency
        latency:
          threshold_ms: 2000

      # conjunction of two predicates inside a single policy
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

      # remainder: probabilistic baseline
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

**Top-level policies are combined disjunctively: one affirmative vote retains the trace.** A policy list therefore cannot narrow a decision; adding a policy can only increase retention. Conjunction is expressed inside a policy with `type: and`, as in `checkout-errors` above, which fires only for traces that are both attributed to the `checkout` service and carry an error status.

## Memory footprint and eviction

`num_traces` and `decision_wait` determine the shape of the memory footprint rather than merely tuning it. Every trace resident during its `decision_wait` window occupies memory, so a rough sizing is `traces_per_second × decision_wait × avg_spans_per_trace × bytes_per_span`. **At 1,000 traces/sec, `decision_wait: 10s`, 10 spans per trace and roughly 1 KB per span, the buffer alone accounts for approximately 100 MB**, before collector overhead. Buffer occupancy tracks the peak arrival rate multiplied by the wait window, so sizing against a mean rate understates the resident set whenever traffic is bursty.

The consequential failure mode is not a crash. **When `num_traces` is exceeded the processor evicts the oldest buffered traces to admit new ones, without regard to whether an evicted trace would have matched an error or latency policy.** An undersized buffer therefore discards precisely the traces the mechanism exists to retain, and it does so when arrival rates spike — which frequently coincides with the incident under investigation. The collector exposes `otelcol_processor_tail_sampling_sampling_trace_dropped_too_early` for this condition; a non-zero value indicates policy evaluation is running on a truncated population.

Cost is relocated rather than removed. Head sampling imposes a small per-span cost inside every service; tail sampling concentrates it in a dedicated memory-bound collector tier that must be provisioned for peak trace concurrency. Whether the exchange is favourable depends on the trace volume at which indiscriminate collection becomes unaffordable, which is a capacity-planning calculation specific to a deployment.

## Pitfalls

- **Multiple tail-sampling replicas without a routing tier.** Symptom: traces arrive at the backend with missing spans and latency policies fire inconsistently. Cause: spans of one trace are spread over several collector instances, and each evaluates policies against its own fragment.
- **Routing on a key other than the trace ID.** Symptom: the load-balancing tier is present, yet traces are still fragmented. Cause: consistent hashing groups spans by whatever `routing_key` names; only `traceID` guarantees co-location of a trace.
- **`decision_wait` shorter than the trace duration.** Symptom: long requests are consistently sampled on partial data. Cause: the timeout is the only completeness signal, and it starts at the first span, so any trace still open when it expires is judged incomplete-but-final.
- **Undersized `num_traces`.** Symptom: `otelcol_processor_tail_sampling_sampling_trace_dropped_too_early` rises during traffic spikes and error traces are absent from the backend. Cause: eviction of the oldest resident traces is indifferent to whether a policy would have retained them.
- **Adding a policy to reduce volume.** Symptom: retention increases after a policy intended to restrict sampling is added. Cause: top-level policies are combined disjunctively; restriction requires `type: and` within a single policy.
- **Sizing the tier by average throughput.** Symptom: the collector is stable in steady state and exhausts memory under burst. Cause: occupancy is governed by arrival rate multiplied by `decision_wait`, so a burst raises the resident set proportionally.
