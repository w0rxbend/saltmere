---
title: "The fourth pillar: continuous profiling in production with Grafana Pyroscope"
date: 2026-07-27
track: observability
summary: "Continuous profiling closes the gap metrics, traces, and logs leave open — why is this service burning CPU right now. A look at Grafana Pyroscope 2.0, eBPF whole-system profiling with Alloy, and OpenTelemetry's new (alpha) profiling signal."
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

Metrics tell you CPU is at 90%. Traces tell you which request is slow. Logs tell you what happened. None of them tell you *which line of code* is burning the CPU. That last question is what continuous profiling answers, and it's why Grafana calls it [the fourth pillar of observability](https://grafana.com/blog/pyroscope-grafana-phlare-join-for-oss-continuous-profiling/), after metrics, logs, and traces.

## What profiling adds beyond the other three

A traditional profiler is something you run for 30 seconds on your laptop when something is already slow. Continuous profiling runs *always*, in production, across every instance, and stores the results in a queryable database. The shift is from "reproduce the problem, then profile" to "the profile of the regression is already sitting in storage, timestamped." When a deploy doubles CPU, you compare the flame graph from before and after and see exactly which function grew.

The reason you can leave it on is **statistical sampling**. Instead of instrumenting every function call, a sampling profiler interrupts each thread ~100 times per second and records the current stack. Over thousands of samples, the functions that appear most often are the ones consuming the most time — and the overhead stays low, typically a couple of percent of CPU, low enough to run fleet-wide.

## Grafana Pyroscope

Pyroscope is the open-source profiling database. In March 2023 Grafana [merged its own Phlare project into Pyroscope](https://grafana.com/blog/pyroscope-grafana-phlare-join-for-oss-continuous-profiling/), keeping the Pyroscope name and front end while adopting Phlare's Loki/Mimir-style storage architecture. The current major line is **[Pyroscope 2.0](https://grafana.com/blog/pyroscope-2-0-release/)** (2026), which reworks the write path so each profile is written to object storage exactly once instead of replicated three times, co-locates and deduplicates symbols from the same service (up to ~95% less symbol storage in Grafana's reports), and makes the read path stateless so any querier can serve any query.

The fastest way to see it work is to run the server and point it at itself:

```bash
docker run -it -p 4040:4040 grafana/pyroscope

# Pyroscope profiles its own Go runtime out of the box.
# Open http://localhost:4040 and you'll see live flame graphs.
```

To profile your own service, add the language SDK. For a Go app:

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

Python, Java, Ruby, .NET, and Node have equivalent SDKs. Each pushes pprof-formatted profiles to the server on an interval.

## Reading a flame graph in ten seconds

A flame graph stacks call frames vertically: the bottom bar is the entry point, each bar above it is a function it called. **Width is what matters** — it's proportional to how many samples included that frame, i.e. how much CPU (or memory) it accounts for. Height is just call depth and means nothing on its own. You read it by scanning for the widest bars near the top: those are the "leaf" functions where time is actually spent. A wide plateau you didn't expect — a JSON serializer, a regex, a lock — is your hotspot. In Pyroscope you can also *diff* two flame graphs across a time range, which colours frames by whether they grew or shrank between two deploys.

## eBPF: profiling without touching the code

SDKs are great for your own services but useless for a Postgres process, an nginx sidecar, or a binary you can't rebuild. This is where **eBPF** whole-system profiling comes in — the same kernel technology behind the auto-instrumentation covered in the journal's [Beyla](/articles/observability/2026-07-24-ebpf-zero-code-instrumentation-beyla-obi) and [Alloy](/articles/observability/2026-07-26-grafana-alloy-collector) pieces. An eBPF profiler samples stacks in the kernel across *every* process on the host, no code changes and no restarts.

Grafana [Alloy](https://grafana.com/docs/alloy/latest/reference/components/pyroscope/pyroscope.ebpf/) ships a `pyroscope.ebpf` component that does exactly this and forwards the profiles to a Pyroscope backend:

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

The component must run as root with host PID namespace access; targets are matched by PID, container ID, or Kubernetes pod labels, and each needs a `service_name`. On Kubernetes you run it as a DaemonSet so every node profiles its own workloads.

## OpenTelemetry's profiling signal — where it stands

Profiling is also becoming a first-class OpenTelemetry signal alongside traces, metrics, and logs. Be precise about maturity here: as of **March 26, 2026 the OTel profiling signal entered public *Alpha*** ([announcement](https://opentelemetry.io/blog/2026/profiles-alpha/)) — not beta, not stable. The [OTLP profiles spec](https://opentelemetry.io/docs/concepts/signals/profiles/) is at v1.10.0, an official eBPF-profiler collector distribution exists (requires Collector v0.148.0+ and supports Go, Node.js, Ruby, .NET, and BEAM), and the announcement explicitly warns it "should not be used for critical production workloads." Beta and GA, plus cross-signal correlation and standardized symbolization, are on the roadmap but not here yet. So for production today, Pyroscope's native ingestion is the mature path; the OTel signal is the direction the whole ecosystem is converging on.

**Try next:** Run `docker run -it -p 4040:4040 grafana/pyroscope`, add the `pyroscope-go` SDK (or the `pyroscope.ebpf` Alloy block) to one service, generate some load, then open the flame graph and diff two five-minute windows to find your widest unexpected frame.
