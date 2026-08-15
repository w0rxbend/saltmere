---
title: "k6: Load Tests as Code, Checks as SLOs"
date: 2026-08-15
track: observability
summary: "Grafana k6 hit 1.0 in May 2025, 2.0 in May 2026, and now sits at 2.2.0: load tests are ES-module JavaScript, thresholds turn latency targets into CI pass/fail gates, and constant-arrival-rate executors model open workloads that don't hide slowdowns behind coordinated omission. Here's the VU-vs-arrival-rate distinction, a full script with an aborting threshold, and the paths to Prometheus remote write and the k6 Operator."
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

A load test that lives in a wiki gets run twice a year; a load test that lives in the repo and fails the pipeline gets run on every merge. That's the entire thesis of **Grafana k6**: tests are JavaScript files, pass/fail criteria are declarative **thresholds**, and the binary exits non-zero when your p95 blows the budget. The project graduated to **1.0 in May 2025** after nine years of v0.x, shipped **2.0 in May 2026** (Playwright-compatible browser APIs, a consolidated extensions catalog, an `expect()` assertions API), and currently sits at **v2.2.0**. The engine is Go with an embedded JS runtime, so a laptop drives tens of thousands of requests per second from a single process.

## VUs, and the lie they can tell

k6's default unit is the **virtual user (VU)**: a loop that runs your `default` function, waits for each response, then iterates. That's a **closed model** — new work starts only when previous work finishes. It faithfully simulates a fixed pool of users, but it has a dangerous property under degradation: when the target slows down, VUs block on responses, so the *request rate drops exactly when the system is struggling*. Your test throttles itself, latency percentiles look survivable, and the report understates the outage. This is **coordinated omission** — Gil Tene's term for measurements that politely stop sampling whenever the system misbehaves.

The fix is an **open model**: arrivals happen on schedule regardless of whether earlier requests came back. In k6 that's the `constant-arrival-rate` and `ramping-arrival-rate` **executors** — you specify iterations per unit time, and k6 allocates VUs from a pool to keep that rate honest. If the target degrades, the rate holds, queues build, and the latency you record is the latency real users would see:

| | Closed model (`ramping-vus`) | Open model (`constant-arrival-rate`) |
|---|---|---|
| You control | number of concurrent users | arrival rate (iters/s) |
| When target slows | offered load silently drops | offered load holds |
| Coordinated omission | yes | avoided |
| Good for | user-pool sims, soak tests | SLO validation, capacity tests |

Rule of thumb: validating an SLO ⇒ open model, always.

## Thresholds are SLOs with an exit code

A **threshold** is a boolean expression over a metric that decides the run's fate — effectively the load-test twin of the SLOs you alert on with [burn-rate alerts](/articles/observability/2026-07-27-slo-burn-rate-alerts). A complete script — open-model scenario, checks, a custom Trend, and a threshold that aborts the run early instead of hammering a dying service for 20 more minutes:

```javascript
import http from "k6/http";
import { check } from "k6";
import { Trend } from "k6/metrics";

const checkoutDuration = new Trend("checkout_duration", true);

export const options = {
  scenarios: {
    checkout: {
      executor: "ramping-arrival-rate",
      startRate: 50, timeUnit: "1s",       // 50 iters/s, ramping up
      preAllocatedVUs: 200, maxVUs: 1000,  // pool k6 may draw from
      stages: [
        { target: 200, duration: "2m" },   // ramp 50→200 iters/s
        { target: 200, duration: "5m" },   // hold
        { target: 0,   duration: "1m" },
      ],
    },
  },
  thresholds: {
    http_req_failed:   ["rate<0.01"],                  // error budget: 1%
    http_req_duration: ["p(95)<300", "p(99)<800"],     // ms
    checkout_duration: [{
      threshold: "p(99)<1500",
      abortOnFail: true, delayAbortEval: "30s",        // stop the run early
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

The division of labor matters: **checks** record pass ratios but never fail the run; **thresholds** decide success — so you gate on `checks{...}` rates or on the built-in `http_req_failed`. If `maxVUs` gets exhausted, k6 emits `dropped_iterations` — threshold that too (`count==0`), because dropped arrivals mean your open model quietly degraded into a closed one.

## Custom metrics, outputs, scale-out

Beyond the built-ins, you get four metric types — `Counter`, `Gauge`, `Rate`, `Trend` — and every metric accepts tags, so thresholds can slice (`http_req_duration{endpoint:checkout}: ["p(95)<300"]`). For where results go, the terminal summary is only the default: `-o experimental-prometheus-rw` streams metrics to any **Prometheus remote write** endpoint (native histograms supported), and 2.0 added a native OpenTelemetry output — meaning your load test emits into the same dashboards, and the same burn-rate math, as production traffic.

Past one machine, the **k6 Operator** (1.0, GA'd September 2025) runs distributed tests on Kubernetes: a `TestRun` custom resource fans one script out across N runner pods. The **browser module** (Chromium via CDP, now with substantially Playwright-compatible APIs) measures Core Web Vitals in the same script that drives protocol load — typical pattern: hundreds of HTTP VUs plus a handful of browser VUs to catch frontend regressions under backend load. And when the built-ins run out, **xk6 extensions** compile Go modules into a custom binary — Kafka, SQL, gRPC streaming, MQTT — with 2.0's catalog marking which are officially maintained.

The failure mode to respect: at high arrival rates, exhaust the *load generator* before you conclude anything about the target — watch k6's own CPU and open-file limits, or your "regression" is a saturated test rig.

**Try next:** pick one endpoint with a written latency SLO, script it with `constant-arrival-rate` at your real production peak rate, set thresholds to the SLO numbers exactly, and wire `k6 run` into CI so the build fails when p95 drifts — then break it on purpose once to confirm the pipeline actually goes red.
