---
title: "Progressive delivery with Argo Rollouts: canary deploys that promote or roll back on their own"
date: 2026-07-26
track: microservices
summary: "The Rollout custom resource replaces the Kubernetes Deployment to ship canaries that shift traffic in weighted steps, query Prometheus between steps, and abort automatically when a success condition is breached."
reading_time: 6
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

**Gist.** A Kubernetes rolling update is binary at the level that matters: once a new pod passes its readiness probe it receives full production traffic, so a regression that appears only under real load is discovered by an alert rather than by the release process. Argo Rollouts replaces the `Deployment` with a `Rollout` custom resource (CR) whose canary strategy advances through **declared traffic weights** and, between weights, runs metric queries whose verdict promotes or aborts the release. The cost is that the release now has duration measured in the analysis interval times the measurement count, and that **the correctness of the gate is entirely the correctness of the query** written into an `AnalysisTemplate`.

James Governor of RedMonk named *progressive delivery* in a 2018 post, framing it as continuous delivery extended with control over the blast radius: release to a small slice of traffic, observe, then widen. Sam Newman treats the same idea in the deployment chapter of *Building Microservices* (2nd ed.), where canary release and parallel run are mechanisms for decoupling deployment from release. Argo Rollouts is the Kubernetes controller that automates the loop.

## The Rollout resource replaces the Deployment

Argo Rollouts ships a controller plus a `Rollout` custom resource that is a near drop-in replacement for a `Deployment`: the same `replicas`, `selector` and `template` fields. What differs is `spec.strategy`. In place of `RollingUpdate` the resource declares `canary` (or `blueGreen`), and the controller manages **two ReplicaSets — stable and canary — shifting traffic between them** rather than replacing pods in place.

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
        - pause: {}          # pause with no duration: wait for manual promote
        - setWeight: 100
```

The step list is the state machine. `setWeight` instructs the traffic router what percentage of requests reaches the canary. `pause` with a `duration` waits for that interval and then advances; **`pause: {}` with no duration halts indefinitely** until an operator runs `kubectl argo rollouts promote checkout`. An `analysis` step interposes a metric verdict between two weights.

## Traffic shaping is delegated, not simulated

Without a traffic router the canary weight is approximated by pod count: **5% is realised as one replica out of twenty**, so the achievable granularity is bounded below by `1/replicas` and small weights force extra pods into existence purely to hit a ratio. Configuring `trafficRouting` against a supported provider — among them Istio, NGINX, AWS Application Load Balancer (ALB) and the Service Mesh Interface (SMI), with further providers such as Gateway API available as plugins — makes the controller write the weight into that provider's own configuration instead. In the example above it patches the Istio `VirtualService` so the mesh splits requests 5/95 between the `checkout-canary` and `checkout-stable` services, independently of how many pods back each one.

## AnalysisTemplate turns metrics into a promote/abort decision

An `AnalysisTemplate` is a reusable query-and-verdict specification. At each `analysis` step the controller creates an `AnalysisRun`, which polls a metric provider on a fixed interval and evaluates each result against a condition expression.

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

The mechanism is a counting rule over a bounded number of samples. Each measurement executes the PromQL query; `successCondition` classifies the result as pass or fail; **every failing measurement increments a failure counter**. Once that counter exceeds `failureLimit` the `AnalysisRun` enters the terminal state **Failed**, and the controller aborts the rollout: traffic returns to 100% stable and the `Rollout` is marked **Degraded**. If the counter stays at or below the limit across all `count` measurements, the run terminates **Successful** and the canary advances to the next step. With `interval: 1m` and `count: 5` the step therefore occupies roughly five minutes of wall-clock time, and the earliest possible abort is after `failureLimit + 1` failing measurements — three of the five in this configuration.

Two structural variants are worth naming. Placing the analysis at `strategy.canary.analysis` rather than in a step runs it **in the background across every weight**, so degradation is detected between checkpoints rather than only at them. Parameterising a template with `args` — for example a `service-name` argument substituted into the query — lets one `AnalysisTemplate` serve many services instead of one copy per service.

The controller's progress is observable from the plugin:

```bash
kubectl argo rollouts get rollout checkout --watch
```

The output reports the current step, the current weight, and the state of each `AnalysisRun`, including the transition to `Degraded` when a metric breaches its condition.

**Try next:** deploy the `checkout` Rollout above against a local `kind` cluster running the Argo Rollouts plugin and a Prometheus, then push an image that returns HTTP 500 on approximately 5% of requests. The watch command shows the breached `successCondition`, the abort at 20% weight, and the reversion to stable. Substituting a latency histogram query (`histogram_quantile(0.95, ...)`) converts the same gate into a p95 latency budget.

## Pitfalls

- **A weight below `1/replicas` is unattainable without a traffic router.** `setWeight: 5` against eight replicas cannot be expressed in pod counts, so the realised split differs from the declared one and the analysis measures a different exposure than intended.
- **A query that does not distinguish canary from stable pods measures the fleet average.** With 5% of traffic on a broken canary, a fleet-wide success rate barely moves and the `successCondition` never trips; the label selectors must isolate the canary workload.
- **Short PromQL ranges over low-traffic services return no data or a noisy ratio.** A `rate(...[2m])` window on a service receiving few requests per minute yields measurements dominated by sampling noise, producing both spurious aborts and missed regressions.
- **`pause: {}` blocks the rollout indefinitely.** A step with no `duration` waits for an explicit `promote` command, so an unattended pipeline stops there rather than completing.
- **An abort returns traffic to stable but leaves the `Rollout` Degraded.** The failed revision is not reverted by deleting anything; the resource remains in the degraded state until a new revision is applied or the rollout is explicitly undone.
- **Background analysis and step analysis have different abort points.** An analysis declared only as a step observes nothing during the weights between steps, so a regression appearing at 50% weight is not detected until the next `analysis` step is reached.
