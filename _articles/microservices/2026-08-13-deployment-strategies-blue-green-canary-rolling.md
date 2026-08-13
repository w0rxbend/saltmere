---
title: "Rolling, Blue-Green, Canary, Dark Launch: A Progressive Delivery Decision Guide"
date: 2026-08-13
track: microservices
summary: "Four ways to ship a new version compared on rollback speed, resource cost, blast radius, and DB-migration risk — plus how automated canary analysis picks the winner without a human at the button."
reading_time: 6
tags: [progressive-delivery, deployment, canary, blue-green, feature-flags, kubernetes]
sources:
  - title: "Kubernetes — Performing a Rolling Update"
    url: "https://kubernetes.io/docs/tutorials/kubernetes-basics/update/update-intro/"
  - title: "Argo Rollouts — Canary & BlueGreen Strategies (docs)"
    url: "https://argo-rollouts.readthedocs.io/en/stable/features/canary/"
  - title: "Argo Rollouts — Analysis & Automated Canary"
    url: "https://argo-rollouts.readthedocs.io/en/stable/features/analysis/"
  - title: "Martin Fowler — Feature Toggles (Feature Flags)"
    url: "https://martinfowler.com/articles/feature-toggles.html"
  - title: "James Governor, RedMonk — Towards Progressive Delivery"
    url: "https://redmonk.com/jgovernor/2018/08/06/towards-progressive-delivery/"
---

Deployment (getting new bits onto servers) and release (letting users hit them) are different events, and progressive delivery is the discipline of decoupling them. Four strategies dominate. They're not ranked — each trades rollback speed against resource cost against blast radius. Here's how to choose.

## The four mechanics

- **Rolling** — Replace old pods with new ones a few at a time behind the same Service. Default in Kubernetes. Cheap (no extra capacity), but the switch is binary per pod: once a new pod passes its readiness probe, it takes full production traffic, and rollback means rolling *backward* pod by pod.
- **Blue-green** — Stand up the full new version (green) beside the old (blue), smoke-test it, then flip the router 100% in one move. Instant rollback (flip back), but you pay for two full environments during the cutover.
- **Canary** — Route a small weighted slice (1% → 10% → 50% → 100%) to the new version, watching metrics between steps. Small blast radius, gradual, but slower and needs traffic-shaping (mesh/ingress) to do properly.
- **Feature flag / dark launch** — Deploy the code disabled, then toggle it on for a cohort at *runtime* — no redeploy. Finest-grained targeting and instant kill switch, at the cost of flag config that lives in your codebase and must be cleaned up.

## Comparison

| | Rollback speed | Extra capacity | Blast radius | Best for |
|---|---|---|---|---|
| **Rolling** | Slow (roll back) | ~0 | Grows as pods flip | Stateless services, low-risk changes |
| **Blue-green** | Instant (router flip) | 2× during cutover | All-or-nothing | Fast cutover + easy rollback, cost OK |
| **Canary** | Fast (shift weight to 0) | +1 small replica set | Tiny, controlled | High-risk changes, rich metrics |
| **Feature flag** | Instant (toggle) | ~0 | Per-user/cohort | Business logic, A/B, kill switches |

## Statefulness and DB migrations

Traffic-shaping assumes both versions can run at once — which breaks against a shared database. Blue-green and canary both put N and N+1 on the same schema simultaneously, so **migrations must be backward-compatible.** Use expand/contract (parallel change): add the new column, backfill and dual-write, migrate readers, *then* drop the old column in a later release. A destructive migration paired with blue-green means the instant "roll back" flips traffic onto a schema the old code can no longer read. Stateful workloads also complicate blue-green: draining in-flight sessions and warming caches on green takes real coordination.

## Automated canary analysis

The point of a canary is to *not* need a human staring at Grafana. Argo Rollouts encodes the promote/abort decision as an `AnalysisTemplate` that queries Prometheus between weight steps; if a metric breaches its bound, the rollout aborts and shifts traffic back to stable automatically.

```yaml
strategy:
  canary:
    steps:
      - setWeight: 10
      - pause: { duration: 5m }
      - analysis:
          templates: [{ templateName: success-rate }]
      - setWeight: 50
      - pause: { duration: 5m }
---
# AnalysisTemplate: abort if success rate drops below 95%
spec:
  metrics:
    - name: success-rate
      successCondition: result[0] >= 0.95
      failureLimit: 2
      provider:
        prometheus:
          query: |
            sum(rate(http_requests_total{status!~"5..",app="checkout"}[2m]))
            / sum(rate(http_requests_total{app="checkout"}[2m]))
```

The analysis is the winner-picker: real production traffic on the canary, real metrics, an objective threshold. No metric to trust yet? A feature flag gives you the same gradual exposure without traffic infrastructure:

```javascript
if (flags.isEnabled("new-checkout", { userId, percentage: 5 })) {
  return newCheckout(cart);
}
return legacyCheckout(cart);
```

## Choosing

Start with **rolling** for routine, low-risk, stateless changes — it's free. Reach for **canary** when a regression would hurt and you have metrics to gate on. Use **blue-green** when you need a clean, instant cutover and can afford double capacity. Layer **feature flags** on top of any of them to control *release* independently of *deploy* — dark-launch a code path, then ramp it per cohort with an instant kill switch. Most mature setups combine them: canary the binary, flag the behavior.

**Try next:** Deploy a service with Argo Rollouts using the canary + analysis config above, then push a version that returns HTTP 500 on ~8% of requests. Confirm the rollout pauses at 10%, the Prometheus success-rate query trips `failureLimit`, and traffic auto-aborts back to stable — without you touching the promote button.
