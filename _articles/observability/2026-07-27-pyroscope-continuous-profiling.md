---
title: "The fourth pillar: continuous profiling in production with Grafana Pyroscope"
date: 2026-07-27
track: observability
summary: "Continuous profiling closes the gap metrics, traces, and logs leave open: which code is burning CPU right now. An account of Grafana Pyroscope 2.0, eBPF whole-system profiling with Alloy, and OpenTelemetry's alpha profiling signal."
reading_time: 6
tags: [profiling, pyroscope, flame-graph, ebpf, opentelemetry, observability]
sources:
  - title: "Pyroscope and Grafana Phlare join together for OSS continuous profiling"
    url: "https://grafana.com/blog/pyroscope-grafana-phlare-join-for-oss-continuous-profiling/"
  - title: "Introducing Pyroscope 2.0: faster, more cost-effective continuous profiling at scale"
    url: "https://grafana.com/blog/pyroscope-2-0-release/"
  - title: "OpenTelemetry Profiles Enters Public Alpha"
    url: "https://opentelemetry.io/blog/2026/profiles-alpha/"
  - title: "pyroscope.ebpf — Grafana Alloy documentation"
    url: "https://grafana.com/docs/alloy/latest/reference/components/pyroscope/pyroscope.ebpf/"
  - title: "Profiles — OpenTelemetry concepts"
    url: "https://opentelemetry.io/docs/concepts/signals/profiles/"
---

**Gist.** Metrics report that a process is at 90% central processing unit (CPU) utilisation, traces report which request was slow, and logs report what happened, but none of the three attributes cost to a specific function in a specific binary. Continuous profiling closes that gap by sampling call stacks on every instance, all the time, and storing the samples in a queryable database, so the profile of a regression exists before anyone goes looking for it. The cost is a permanent sampling overhead on every profiled process — small, because it scales with the sampling rate rather than with call frequency, but never zero — plus a storage and symbolisation pipeline that must absorb stack traces from the whole fleet.

## What profiling adds beyond the other three

A conventional profiler is an interactive tool: it is attached for tens of seconds to a process that is already known to be slow, usually on a workstation, usually after the fault has been reproduced. That workflow has a precondition that production rarely satisfies — the fault must be reproducible on demand. Continuous profiling inverts the order. Because collection is always on and every sample is timestamped and labelled by service and instance, the investigation begins with data that was recorded during the incident rather than after it. A deploy that doubles CPU is diagnosed by comparing the flame graph of the window before the rollout with the window after it and identifying the frame whose share of samples grew.

Grafana positions this as [the fourth pillar of observability](https://grafana.com/blog/pyroscope-grafana-phlare-join-for-oss-continuous-profiling/), after metrics, logs, and traces.

### Why always-on collection is affordable

The property that makes permanent collection viable is **statistical sampling**. An instrumenting profiler records an event at every function entry and exit, so its overhead scales with call frequency and is unbounded for call-heavy code. A sampling profiler instead interrupts each thread at a fixed rate — tens of times per second; the Go runtime profiler defaults to 100 hertz, Alloy's eBPF profiler to 19 — and records only the current stack. Cost therefore scales with the sampling frequency and the stack depth, not with how often the program calls functions.

The consequence for interpretation matters as much as the consequence for cost. A sampling profile is an **estimate of the distribution of time across stacks**, not a count of calls. A function that appears in a large fraction of samples was on the stack for a large fraction of wall or CPU time; a function that never happens to be executing at an interrupt boundary does not appear at all. Estimates therefore tighten as samples accumulate, which is precisely why long observation windows in production can be more informative than a 30-second local capture: the estimator has more draws.

## Grafana Pyroscope

Pyroscope is the open-source database for these profiles. On 16 March 2023 Grafana announced that it was [merging the Pyroscope project and Grafana Phlare](https://grafana.com/blog/pyroscope-grafana-phlare-join-for-oss-continuous-profiling/) under the single name Grafana Pyroscope.

The current major line is **[Pyroscope 2.0](https://grafana.com/blog/pyroscope-2-0-release/)**, which ran in Grafana Cloud Profiles — reaching all regions in September 2025 — before the open-source release. Three changes are load-bearing:

- **Each profile is written to object storage exactly once** instead of being replicated three times on the write path.
- **Symbols from the same service are co-located and deduplicated.** Stack frames from many instances of one binary share the same function names and file paths; Grafana reports that co-locating and deduplicating symbols cut the symbol storage footprint **by up to 95% in its own production environment**.
- **The read path is stateless**, so any querier can serve any query rather than only queries whose data it happens to own.

The server profiles its own Go runtime, so a single container is enough to see the pipeline end to end:

```bash
docker run -it -p 4040:4040 grafana/pyroscope

# Pyroscope profiles its own Go runtime out of the box.
# The user interface and live flame graphs are at http://localhost:4040
```

An application is profiled by linking a language software development kit (SDK). For a Go service:

```go
import "github.com/grafana/pyroscope-go"

pyroscope.Start(pyroscope.Config{
    ApplicationName: "checkout.service",
    ServerAddress:   "http://localhost:4040",
    ProfileTypes: []pyroscope.ProfileType{
        pyroscope.ProfileCPU,
        pyroscope.ProfileAllocObjects,
        pyroscope.ProfileInuseSpace,
    },
})
```

Python, Java, Ruby, .NET, and Node.js have equivalent SDKs. Each pushes **pprof-formatted profiles** to the server on an interval, so the wire format is shared across languages and the server does not need per-language decoding.

## Reading a flame graph

A flame graph stacks call frames vertically: the bottom bar is the entry point and each bar above it is a function that the bar below called. **Width is the only quantitative axis** — it is proportional to the number of samples that contained that frame, and therefore to the resource (CPU time, allocated objects, resident bytes) attributed to it. **Height carries no magnitude**; it is call depth alone, so a deep tower of narrow frames is cheap and a shallow wide plateau is expensive.

Reading proceeds from the widest bars near the top of the graph. Those are the leaf frames, where a sample was executing rather than waiting on a callee, and an unexpected wide plateau — a serialiser, a regular-expression engine, a lock — is the hotspot. Pyroscope additionally **diffs two flame graphs across two time ranges**, colouring frames by whether their sample share grew or shrank, which turns a before/after deploy comparison into a single view.

## eBPF: profiling without modifying the binary

An SDK requires the ability to rebuild and redeploy the process. That excludes a Postgres instance, an nginx sidecar, or any third-party binary. **Extended Berkeley Packet Filter (eBPF)** profiling removes the requirement: the profiler samples stacks in the kernel across *every* process on the host, with no code change and no restart. It is the same kernel facility behind the auto-instrumentation covered in the journal's [Beyla](/articles/observability/2026-07-24-ebpf-zero-code-instrumentation-beyla-obi) and [Alloy](/articles/observability/2026-07-26-grafana-alloy-collector) pieces.

Grafana Alloy ships a [`pyroscope.ebpf`](https://grafana.com/docs/alloy/latest/reference/components/pyroscope/pyroscope.ebpf/) component that collects such profiles and forwards them to a Pyroscope backend:

```alloy
pyroscope.ebpf "default" {
  forward_to = [pyroscope.write.endpoint.receiver]
  targets    = [{__process_pid__ = "1", service_name = "checkout.service"}]
}

pyroscope.write "endpoint" {
  endpoint {
    url = "http://pyroscope:4040"
  }
}
```

Two constraints govern deployment. The component **must run as root with access to the host process identifier (PID) namespace**, because it resolves and samples processes outside its own container. And **every target should carry a `service_name`**; where it is neither set nor inferred from container metadata it defaults to `unspecified`, and the service name is what groups the resulting profiles. Each target is identified by one of `__process_pid__`, `__container_id__`, `__meta_docker_container_id__`, or `__meta_kubernetes_pod_container_id__`. The component samples at 19 stack traces per second by default and ships a batch every 15 seconds. On Kubernetes the component is deployed as a DaemonSet so that each node profiles the workloads scheduled onto it.

## The OpenTelemetry profiling signal

Profiling is becoming a first-class OpenTelemetry signal alongside traces, metrics, and logs, and its maturity should be stated exactly. The signal has [entered public *Alpha*](https://opentelemetry.io/blog/2026/profiles-alpha/) — not beta, not stable — and the [OpenTelemetry Protocol (OTLP) profiles](https://opentelemetry.io/docs/concepts/signals/profiles/) messages are still published under the `v1development` proto package rather than a stable `v1`. An official eBPF-profiler Collector distribution exists; it requires **Collector v0.148.0 or newer** and covers Go, Node.js/V8, Ruby, .NET, and BEAM, plus any runtime on Linux through the eBPF agent. The announcement states the signal "should not be used for critical production workloads". Beta and general availability, together with cross-signal correlation and standardised symbolisation, are on the roadmap and not yet delivered.

The practical consequence is a split: Pyroscope's native ingestion is the mature production path today, while the OTLP signal is the interface the wider ecosystem is converging on.

## Pitfalls

- **Reading height instead of width.** A tall flame graph looks alarming but only encodes recursion or deep call chains; a single wide leaf frame at shallow depth is the expensive one.
- **Treating a sampling profile as a call count.** Frames absent from the graph are not proven absent from execution — they were never on the stack at an interrupt, which for short, frequently-called functions is likely.
- **Drawing conclusions from a short window.** With sampling at tens of hertz per thread, a narrow time range yields few samples and frame widths that are dominated by sampling noise rather than by real cost differences.
- **Deploying `pyroscope.ebpf` without root or host PID namespace access.** The component cannot enumerate or sample processes outside its own namespace, so the configuration is accepted and the profiles never arrive.
- **Omitting `service_name` on an eBPF target.** Where the label is neither set nor inferred from container metadata it defaults to `unspecified`, so unrelated workloads are aggregated into one service in queries and diffs.
- **Running `pyroscope.ebpf` as a Deployment on Kubernetes.** A single replica profiles only the node it landed on; whole-fleet coverage requires a DaemonSet.
- **Depending on the OpenTelemetry profiling signal for production incident response.** It is alpha, its own announcement advises against critical production workloads, and symbolisation is not yet standardised across implementations.
