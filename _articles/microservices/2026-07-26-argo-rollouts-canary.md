---
title: "Progressive delivery with Argo Rollouts: canary deploys that promote or roll back on their own"
date: 2026-07-26
track: microservices
summary: "Replace the Kubernetes Deployment with a Rollout CRD to ship canaries that shift traffic in weighted steps, query Prometheus between steps, and auto-abort when the numbers look wrong — no human at the promote button."
reading_time: 5
tags: [progressive-delivery, canary, argo-rollouts, kubernetes, prometheus, analysis]
sources:
  - title: "Argo Rollouts Releases (GitHub)"
    url: "https://github.com/argoproj/argo-rollouts/releases"
  - title: "Argo Rollouts — Analysis & Progressive Delivery (docs)"
    url: "https://argo-rollouts.readthedocs.io/en/stable/features/analysis/"
  - title: "Argo Rollouts — analysis.md (source)"
    url: "https://github.com/argoproj/argo-rollouts/blob/master/docs/features/analysis.md"
  - title: "James Governor, RedMonk — Towards Progressive Delivery"
    url: "https://redmonk.com/jgovernor/2018/08/06/towards-progressive-delivery/"
  - title: "Sam Newman, Building Microservices (2nd ed.), Ch. 8 — Deployment"
    url: "https://samnewman.io/books/building_microservices_2nd_edition/"
---

A rolling update is binary at the level that matters: once a new pod passes its readiness probe, it takes full production traffic. If the regression only shows up under real load — a p99 that creeps, an error rate that climbs at 3% of requests — the rollout keeps marching and you find out from your pager.

Progressive delivery is the fix. James Governor of RedMonk coined the term in 2018, framing it as "Continuous Delivery++": you deploy to a small slice of traffic, control the *blast radius*, watch the metrics, then widen. Sam Newman folds the same idea into the deployment chapter of *Building Microservices* — canary release and parallel run as ways to decouple deployment from release. Argo Rollouts is the Kubernetes controller that automates the loop.

## The Rollout CRD replaces your Deployment

Argo Rollouts (current stable on the **v1.9** line, released March 2026) ships a controller plus a `Rollout` custom resource that is a near drop-in for a `Deployment`: same `replicas`, `selector`, and `template`. What changes is `spec.strategy` — instead of `RollingUpdate` you declare `canary` (or `blueGreen`), and the controller manages two ReplicaSets, shifting traffic between them.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: checkout
spec:
  replicas: 8
  selector:
    matchLabels: { app: checkout }
  template:
    metadata:
      labels: { app: checkout }
    spec:
      containers:
        - name: checkout
          image: registry.internal/checkout:2.4.0
          ports: [{ containerPort: 8080 }]
  strategy:
    canary:
      canaryService: checkout-canary   # service selecting only canary pods
      stableService: checkout-stable   # service selecting only stable pods
      trafficRouting:
        istio:
          virtualService:
            name: checkout-vsvc
            routes: [primary]
      steps:
        - setWeight: 5
        - pause: { duration: 2m }
        - setWeight: 20
        - analysis:
            templates:
              - templateName: success-rate
        - setWeight: 50
        - pause: {}          # pause with no duration = wait for manual promote
        - setWeight: 100
```

`setWeight` tells the traffic router what percentage of requests hit the canary. `pause` with a `duration` waits and moves on; `pause: {}` with no duration halts until a human runs `kubectl argo rollouts promote checkout`. Between weight bumps you drop in an `analysis` step — that is where the automation earns its keep.

## Traffic shaping is delegated, not simulated

Without a traffic router, the canary weight is approximated by pod count (5% ≈ 1 of 20 replicas). That is coarse and wasteful. Point `trafficRouting` at a mesh or ingress — Istio, Linkerd, NGINX, AWS ALB, Gateway API — and Argo Rollouts writes the actual weight into that provider's config. In the example above it patches the Istio `VirtualService` so the mesh splits requests 5/95 between the `checkout-canary` and `checkout-stable` services. Real traffic, real percentages, no extra pods spun up just to hit a ratio.

## AnalysisTemplate turns metrics into a promote/abort decision

An `AnalysisTemplate` is a reusable query-and-verdict spec. At each `analysis` step the controller spawns an `AnalysisRun` that polls a metric provider on an interval and compares results against a condition. Here is a Prometheus-backed success-rate check:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: AnalysisTemplate
metadata:
  name: success-rate
spec:
  metrics:
    - name: success-rate
      interval: 1m          # query every minute
      count: 5              # take 5 measurements, then decide
      failureLimit: 2       # allow 2 bad readings before failing
      successCondition: result[0] >= 0.99
      provider:
        prometheus:
          address: http://prometheus.monitoring:9090
          query: |
            sum(rate(http_requests_total{
                  app="checkout", code!~"5.."}[2m]))
            /
            sum(rate(http_requests_total{app="checkout"}[2m]))
```

The mechanics: each measurement runs the PromQL, `successCondition` decides pass/fail, and every failing reading increments a counter. Cross `failureLimit` and the `AnalysisRun` goes **Failed** — the controller aborts the rollout, shifts traffic back to 100% stable, and marks the Rollout **Degraded**. Stay under it for all `count` measurements and the run is **Successful**, so the canary proceeds to the next step. No dashboards watched, no manual judgment call — the SLO *is* the gate.

Two patterns worth knowing. Run analysis in the *background* (`strategy.canary.analysis` rather than a step) to monitor continuously across every weight, aborting the moment metrics degrade instead of only at checkpoints. And parameterize templates with `args` (e.g. `service-name`) so one `AnalysisTemplate` serves every service in the fleet.

Watch it live:

```bash
kubectl argo rollouts get rollout checkout --watch
```

You'll see each step, the current weight, and the `AnalysisRun` verdict stream past — and, when a metric breaches, the automatic rollback happen without you touching anything.

**Try next:** Deploy the `checkout` Rollout above against a local `kind` cluster with the Argo Rollouts plugin and a Prometheus, then push an image build that returns HTTP 500 on ~5% of requests. Watch `kubectl argo rollouts get rollout checkout --watch` catch the breached `successCondition`, abort at 20% weight, and revert to stable — then flip the query to a latency histogram (`histogram_quantile(0.95, ...)`) and gate on p95 instead.
