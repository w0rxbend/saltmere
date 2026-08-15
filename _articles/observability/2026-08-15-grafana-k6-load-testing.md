---
title: "k6: Load Tests as Code, Checks as SLOs"
date: 2026-08-15
track: observability
summary: "Grafana k6 reached 1.0 in May 2025 and 2.0 in May 2026: load tests are ES-module JavaScript, thresholds turn latency targets into CI pass/fail gates, and constant-arrival-rate executors model open workloads that do not hide slowdowns behind coordinated omission. Covers the VU-versus-arrival-rate distinction, a full script with an aborting threshold, and the paths to Prometheus remote write and the k6 Operator."
reading_time: 6
tags: [k6, load-testing, slo, thresholds, performance, grafana, ci]
sources:
  - title: "k6 documentation — Thresholds"
    url: "https://grafana.com/docs/k6/latest/using-k6/thresholds/"
  - title: "k6 documentation — Open and closed models"
    url: "https://grafana.com/docs/k6/latest/using-k6/scenarios/concepts/open-vs-closed/"
  - title: "Grafana k6 1.0 release announcement"
    url: "https://grafana.com/blog/2025/05/07/grafana-k6-1.0-release/"
  - title: "k6 2.0 release announcement"
    url: "https://grafana.com/blog/k6-2-0-release/"
  - title: "Grafana k6 Operator 1.0 announcement"
    url: "https://grafana.com/blog/distributed-performance-testing-for-kubernetes-environments-grafana-k6-operator-1-0-is-here/"
---

**Gist.** A load test whose pass criterion lives in a human's judgement is run rarely and interpreted charitably; a load test whose pass criterion is a declarative expression over a metric can gate a pipeline. **Grafana k6** encodes the scenario as a JavaScript module and the criterion as a **threshold**, and exits with a non-zero status code when the threshold is violated. The cost is that the measurement is only as honest as the arrival model chosen: a closed model throttles itself precisely when the target degrades, so the recorded latency understates the incident.

Grafana k6 reached **1.0 in May 2025** after a long sequence of v0.x releases, and shipped **2.0 in May 2026** (Playwright-compatible browser application programming interfaces, a consolidated extensions catalogue, an `expect()` assertions API). The engine is written in Go with an embedded JavaScript runtime, so a single process drives high request rates without one operating-system thread per simulated user.

## Virtual users and the closed model

The default unit of execution is the **virtual user (VU)**: a loop that invokes the exported `default` function, waits for each response, and then iterates. That is a **closed model** — a new unit of work begins only after the previous one completes. Concurrency is the controlled variable; **throughput is an output, not an input**.

The consequence under degradation is structural rather than incidental. If a VU's iteration takes *d* seconds, that VU offers 1/*d* iterations per second, so a pool of *n* VUs offers *n*/*d*. When the target slows and *d* doubles, the offered rate halves. **The load generator reduces pressure exactly when the system under test is failing**, and every subsequent latency sample is drawn from a system that is no longer being asked for the original workload. The percentiles that result describe an experiment the test itself altered. This is **coordinated omission**, Gil Tene's term for a measurement process that stops sampling whenever the system misbehaves.

## The open model and arrival-rate executors

An **open model** decouples arrivals from completions: work is started on a schedule regardless of whether earlier work has returned. In k6 this is expressed by the `constant-arrival-rate` and `ramping-arrival-rate` **executors**, which take iterations per unit of time as the controlled variable. k6 draws VUs from a pre-allocated pool to sustain that rate; if the target degrades, the rate is held, queueing accumulates, and the recorded latency includes the queueing delay a real arrival would have experienced.

| | Closed model (`ramping-vus`) | Open model (`constant-arrival-rate`) |
|---|---|---|
| Controlled variable | number of concurrent users | arrival rate (iterations/s) |
| When target slows | offered load drops | offered load holds |
| Coordinated omission | present | avoided |
| Suited to | fixed user-pool simulation, soak tests | SLO validation, capacity tests |

The invariant that makes the open model trustworthy is that **the VU pool never binds**. When the pool is exhausted, k6 cannot start a scheduled iteration and records it in the `dropped_iterations` metric. Dropped iterations mean the run silently reverted to a closed model for the duration of the shortfall, and the latency distribution from that window is not comparable to the rest.

## Thresholds as a pass/fail contract

A **threshold** is an expression over an aggregate of a metric — a rate, a count, a percentile of a trend — evaluated at the end of the run. Violation sets a non-zero exit status, which is what makes the test usable as a continuous-integration gate; it is the load-test counterpart of the service-level objectives monitored with [burn-rate alerts](/articles/observability/2026-07-27-slo-burn-rate-alerts).

The division of responsibility is the detail most often misread. **A `check` records a pass ratio and never fails the run**; it produces the `checks` metric and nothing more. Only a threshold decides the outcome, so gating on assertion results requires a threshold over the `checks` rate or over the built-in `http_req_failed`.

The following script combines an open-model scenario, checks, a custom `Trend`, and a threshold configured with `abortOnFail` so a badly degraded target terminates the run rather than absorbing the remaining stages.

```javascript
import http from "k6/http";
import { check } from "k6";
import { Trend } from "k6/metrics";

const checkoutDuration = new Trend("checkout_duration", true);

export const options = {
  scenarios: {
    checkout: {
      executor: "ramping-arrival-rate",
      startRate: 50, timeUnit: "1s",       // 50 iterations/s at start
      preAllocatedVUs: 200, maxVUs: 1000,  // pool k6 may draw from
      stages: [
        { target: 200, duration: "2m" },   // ramp 50 -> 200 iterations/s
        { target: 200, duration: "5m" },   // hold
        { target: 0,   duration: "1m" },
      ],
    },
  },
  thresholds: {
    http_req_failed:     ["rate<0.01"],                // 1% error budget
    http_req_duration:   ["p(95)<300", "p(99)<800"],   // milliseconds
    dropped_iterations:  ["count==0"],                 // pool never bound
    checks:              ["rate>0.99"],                // checks do not gate alone
    checkout_duration: [{
      threshold: "p(99)<1500",
      abortOnFail: true, delayAbortEval: "30s",        // ignore warm-up window
    }],
  },
};

export default function () {
  const res = http.post("https://test.example.com/checkout",
    JSON.stringify({ sku: "A-42", qty: 1 }),
    { headers: { "Content-Type": "application/json" } });
  checkoutDuration.add(res.timings.duration);
  check(res, {
    "status 200": (r) => r.status === 200,
    "has order id": (r) => r.json("orderId") !== undefined,
  });
}
```

`delayAbortEval` suppresses threshold evaluation for the stated interval after the scenario starts, so a cold cache or an unwarmed connection pool does not abort a run in its first seconds.

## Custom metrics, outputs and distribution

Beyond the built-in metrics, four custom metric types are available — `Counter`, `Gauge`, `Rate` and `Trend` — and every metric accepts tags, so a threshold can be scoped to a subset of samples (`http_req_duration{endpoint:checkout}: ["p(95)<300"]`). The terminal summary is the default output only. The `prometheus-rw` output streams metrics to a **Prometheus remote write** endpoint, with native histograms supported, and an OpenTelemetry output is also available. The consequence is that load-test series land in the same storage, and are queried with the same expressions, as production telemetry.

Beyond one machine, the **k6 Operator** (1.0) executes distributed tests on Kubernetes: a `TestRun` custom resource distributes one script across a set of runner pods. The **browser module** drives Chromium over the Chrome DevTools Protocol (CDP) with substantially Playwright-compatible APIs, allowing frontend measurements to be collected in the same script that generates protocol-level load. Where the built-in protocols are insufficient, **xk6 extensions** compile Go modules into a custom k6 binary — Kafka, SQL, gRPC streaming, MQTT — and the 2.0 catalogue records which extensions are officially maintained.

## Pitfalls

- **A `check` that fails leaves the exit status at zero.** Checks only populate the `checks` metric; without a threshold over that metric or over `http_req_failed`, a pipeline reports success while every assertion fails.
- **A non-zero `dropped_iterations` count invalidates the latency percentiles for that window.** The VU pool bound, so arrivals were skipped and the scenario degraded from an open model into a closed one — the very condition the arrival-rate executor was chosen to avoid.
- **`ramping-vus` results cannot be used to validate a latency SLO.** Under degradation the offered rate falls with the VU iteration time, so the percentiles describe a reduced workload rather than the target one.
- **Saturating the load generator produces a regression that exists only in the test rig.** At high arrival rates the k6 process itself can exhaust CPU or the open-file-descriptor limit, inflating client-side timings; generator CPU and file-descriptor usage must be recorded alongside the target's.
- **`abortOnFail` without `delayAbortEval` can terminate a run during warm-up.** Percentiles computed over the first few samples are volatile, so a cold start can trip a threshold that steady-state traffic would satisfy.
- **An untagged threshold aggregates every request in the script.** A single slow endpoint raises the global `http_req_duration` percentile and fails a budget written for a different endpoint; scoping requires a tag selector.
