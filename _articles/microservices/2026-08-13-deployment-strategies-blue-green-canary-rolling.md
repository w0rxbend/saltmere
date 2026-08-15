---
title: "Rolling, Blue-Green, Canary, Dark Launch: A Progressive Delivery Decision Guide"
date: 2026-08-13
track: microservices
summary: "Four ways to ship a new version compared on rollback speed, resource cost, blast radius, and database-migration risk, with automated canary analysis as the mechanism that decides promote or abort without a human at the button."
reading_time: 8
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

**Gist.** Deployment (placing new binaries on servers) and release (allowing user traffic to reach them) are distinct events, and progressive delivery — the framing James Governor set out in *Towards Progressive Delivery* — is the practice of separating them so that exposure can be increased or withdrawn independently of the artefact rollout. The mechanism in every case is a control point that decides what fraction of requests reaches version N+1: a rolling replacement schedule, a router switch, a traffic weight, or a runtime predicate inside the process. Each control point buys a different rollback latency, and each is paid for in extra capacity, extra traffic-shaping infrastructure, or extra code that must later be removed.

## Four control points

- **Rolling.** Old pods are replaced by new ones a few at a time behind the same Service. This is the default Kubernetes Deployment strategy. It requires no additional capacity, but the control point is per pod and binary: **once a new pod passes its readiness probe it is added to the Service endpoints and receives a full share of production traffic.** Exposure is therefore the ratio of updated pods to total pods, and it is not directly steerable. Rollback is a second rolling update in the opposite direction, so recovery time is bounded below by the time to start and make ready a replica set of old pods.
- **Blue-green.** The complete new version (green) is brought up alongside the running one (blue), verified out of band, and then the router is moved to green in a single operation. Rollback is the inverse operation on the router, so recovery is as fast as a routing change. The cost is that **both environments are fully provisioned during the cutover window**, roughly doubling capacity for that period.
- **Canary.** A weighted slice of traffic is directed to the new version and increased in steps (for example 10 percent, then 50 percent, then 100 percent), with metrics evaluated between steps. Blast radius during any step is bounded by the current weight. The cost is elapsed time and a dependency on traffic-shaping infrastructure — a service mesh or an ingress controller able to split traffic by weight between two backends.
- **Feature flag / dark launch.** The code ships disabled and is enabled at runtime for a chosen cohort, with no redeployment. The control point is a predicate evaluated per request, which gives the finest targeting granularity and an immediate kill switch. The cost is that the flag and both code paths live in the codebase and must be removed later; Fowler's *Feature Toggles* treats that removal as part of the toggle's lifecycle rather than optional cleanup.

## Comparison

| | Rollback speed | Extra capacity | Blast radius | Suited to |
|---|---|---|---|---|
| **Rolling** | Slow (reverse rollout) | ~0 | Grows as pods flip | Stateless services, low-risk changes |
| **Blue-green** | Router flip | 2× during cutover | All-or-nothing | Fast cutover with easy rollback |
| **Canary** | Shift weight to 0 | +1 small replica set | Bounded by current weight | High-risk changes with usable metrics |
| **Feature flag** | Toggle | ~0 | Per user or cohort | Business logic, experiments, kill switches |

## The shared-state invariant

Every strategy other than a full stop-the-world cutover rests on one invariant: **versions N and N+1 must be able to run concurrently against the same persistent state.** Rolling, blue-green during the cutover window, and canary at every intermediate weight all place both versions in front of the same store at once, so each is correct only while this holds.

The invariant is broken most often by schema change. A migration that drops or renames a column makes the old code unable to read the store, which destroys the rollback path precisely when it is needed: traffic flips back to version N and version N alone cannot serve. The standard remedy is expand/contract, also called parallel change — add the new column, backfill it, write to both old and new shapes, move readers to the new shape, and only in a later release, after rollback to the previous version is no longer wanted, drop the old column. Each step in that sequence is individually reversible; the combined migration is not.

Statefulness constrains blue-green in a second way. In-flight sessions on blue do not migrate when the router moves, so either the sessions are externalised or the cutover must drain them. Caches on green start cold, so the first traffic after a flip sees whatever miss-path latency the service has, on 100 percent of requests at once. A canary reaches the same steady state gradually and exposes cold-cache behaviour at a bounded weight first.

## Automated canary analysis

A canary whose promote/abort decision depends on a human reading a dashboard inherits that human's response time. Argo Rollouts encodes the decision as an `AnalysisTemplate` evaluated between weight steps: the template queries a metrics provider such as Prometheus, and **if the measurement fails its condition more times than `failureLimit` permits, the rollout aborts and traffic returns to the stable version.** The rollout is a state machine over the step list, and analysis is the guard on each transition.

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

Two properties make this an objective gate rather than a ritual. The numerator and denominator are both scoped to a rolling two-minute window, so the ratio reflects current behaviour rather than a lifetime average that a short burst of errors cannot move. And `failureLimit: 2` tolerates a bounded number of failing evaluations, which distinguishes a sustained regression from a single scrape landing on a transient.

Where no metric is yet trustworthy enough to gate on, a feature flag provides graded exposure without any traffic-shaping infrastructure, because the control point moves inside the process.

### Implementation sketch (Scala)

The load-bearing property of a percentage rollout is not randomness but **stability**: the same subject must receive the same answer on every request, or a user will oscillate between the old and new code paths across page loads. Hashing the subject together with the flag name gives both stability and independence between flags.

```scala
final case class Rule(flag: String, percentage: Int, forced: Set[String])

object Toggle:
  /** Bucket in [0, 100). Salted with the flag name so two 5% flags
    * do not select the same users. */
  private def bucket(flag: String, subject: String): Int =
    val h = java.security.MessageDigest
      .getInstance("SHA-256")
      .digest(s"$flag:$subject".getBytes("UTF-8"))
    // First four digest bytes as an unsigned 32-bit value, folded into 0..99.
    val v = ((h(0) & 0xffL) << 24) | ((h(1) & 0xffL) << 16) |
            ((h(2) & 0xffL) << 8) | (h(3) & 0xffL)
    (v % 100L).toInt

  def isEnabled(rule: Rule, subject: String): Boolean =
    rule.forced.contains(subject) || bucket(rule.flag, subject) < rule.percentage

// Call site: both paths remain present until the toggle is retired.
def checkout(cart: Cart, userId: String, rules: Map[String, Rule]): Receipt =
  rules.get("new-checkout") match
    case Some(r) if Toggle.isEnabled(r, userId) => newCheckout(cart)
    case _                                      => legacyCheckout(cart)
```

Raising `percentage` from 5 to 10 is monotonic under this scheme: every subject already enabled stays enabled, because its bucket is unchanged and the threshold only moves upward. A scheme that reshuffles buckets when the percentage changes loses that property and re-randomises the exposed cohort at every ramp step.

## Choosing

Rolling is the default for routine, stateless, low-risk changes, since it consumes no extra capacity. Canary applies where a regression carries real cost and a metric exists that would detect it within the pause window. Blue-green applies where a clean, single-moment cutover matters more than the doubled capacity it costs. Feature flags compose with all three, because they act at a different layer: the binary can be rolled out by any of the first three mechanisms while the behaviour it contains stays dark until toggled.

**Try next:** deploy a service with Argo Rollouts using the canary and analysis configuration above, then push a version returning HTTP 500 on a small fraction of requests. The expected sequence is that the rollout holds at weight 10, the success-rate query falls below 0.95 on successive evaluations, `failureLimit` is exceeded, and traffic returns to stable with no manual promotion or abort.

## Pitfalls

- A readiness probe that only checks process liveness marks a pod ready before its dependencies are usable; the rolling update then continues replacing pods while each new one returns errors, and the rollout completes successfully with a broken service.
- A destructive migration applied before a blue-green cutover leaves the old version unable to read the store; the router flip back succeeds, but blue then fails on every request, so the rollback path exists in the router and not in the data.
- A canary weight far below the noise floor of the metric produces an analysis that cannot fail: at low weight the canary contributes too few requests for the error ratio to move outside its normal variation, and every step passes regardless of the new version's behaviour.
- A Prometheus query whose selector matches both the canary and the stable pods dilutes canary errors with stable successes, so the success condition holds while the canary is failing.
- Session affinity configured on the router pins existing users to the stable version, so a canary at weight 10 exposes only new sessions, and the analysis window measures a population unrepresentative of production.
- A cold cache on green turns a blue-green flip into a latency spike on all traffic simultaneously, which a synthetic smoke test against green does not reproduce because the test itself warms only the paths it exercises.
- Feature flags left in place after full rollout accumulate as dead branches whose disabled path is no longer tested; when the flag is later toggled off during an incident, the code it re-enables has drifted out of working order.
