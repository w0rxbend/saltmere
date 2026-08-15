---
title: "KEDA: scaling a consumer on queue depth — including down to zero"
date: 2026-07-30
track: sys-patterns
summary: "KEDA drives the Kubernetes Horizontal Pod Autoscaler off queue depth and event-source metrics, and — unlike the stock HPA on external metrics alone — scales a consumer to zero and back."
reading_time: 6
tags: [keda, autoscaling, kubernetes, hpa, kafka, rabbitmq, event-driven, burns]
sources:
  - title: "KEDA Concepts — architecture (operator, metrics adapter, admission webhooks)"
    url: "https://keda.sh/docs/latest/concepts/"
  - title: "Scaling Deployments, StatefulSets & Custom Resources — KEDA docs"
    url: "https://keda.sh/docs/latest/concepts/scaling-deployments/"
  - title: "Apache Kafka scaler — KEDA docs"
    url: "https://keda.sh/docs/latest/scalers/apache-kafka/"
  - title: "CNCF Announces Graduation of Kubernetes Autoscaler KEDA (Aug 22, 2023)"
    url: "https://www.cncf.io/announcements/2023/08/22/cloud-native-computing-foundation-announces-graduation-of-kubernetes-autoscaler-keda/"
  - title: "KEDA Releases (kedacore/keda, GitHub)"
    url: "https://github.com/kedacore/keda/releases"
---

**Gist.** A queue consumer's load is backlog, not central processing unit (CPU) utilisation: ten thousand unprocessed Kafka messages warrant more pods even when every existing pod sits idle on a slow downstream, and an empty queue warrants zero. The Kubernetes Horizontal Pod Autoscaler (HPA) reads external metrics only through an adapter that must be built and wired separately, and it will not scale a workload to zero on those metrics in a default cluster; [KEDA](https://keda.sh/docs/latest/concepts/) — Kubernetes Event-Driven Autoscaling — supplies the adapter and owns the zero transitions itself. The cost is the cold start and, for consumer groups, the partition rebalance that every departure from zero and return to it incurs.

Brendan Burns' *Designing Distributed Systems* describes the **replicated, load-balanced service** — N identical stateless replicas behind a balancer — as the base serving pattern. What that pattern leaves open is the signal the replica count is derived from. For request-driven services the signal is CPU or requests per second, which the stock HPA handles directly; for a queue consumer it is backlog, which it does not. KEDA is a Cloud Native Computing Foundation (CNCF) **graduated** project, having graduated on [22 August 2023](https://www.cncf.io/announcements/2023/08/22/cloud-native-computing-foundation-announces-graduation-of-kubernetes-autoscaler-keda/); the [releases](https://github.com/kedacore/keda/releases) are on the 2.x line, and the documentation's `latest` tracks the newest of them.

## Components and the division of responsibility

KEDA presents itself as a lightweight *complement* to Kubernetes rather than a replacement. It installs three components:

- **keda-operator** (the agent) — watches `ScaledObject` and `ScaledJob` resources and manages the HPA lifecycle. It handles the **0↔1 transition directly**: it activates a scaled-to-zero deployment when events arrive and deactivates it back to zero when the source goes idle.
- **keda-metrics-apiserver** — a Kubernetes external-metrics adapter. It polls the event source (Kafka, RabbitMQ, Prometheus, Amazon Simple Queue Service, and others) and exposes the resulting values to the HPA through the standard external metrics application programming interface (API).
- **keda-admission-webhooks** — validates each resource at apply time, so a malformed trigger is rejected rather than accepted and left inert.

The load-bearing invariant is the split: **KEDA owns 0→1 and 1→0; the HPA owns 1→N.** The two never contend, because KEDA feeds the HPA rather than acting on the same replica field. Once at least one replica runs, an ordinary HPA — created and owned by KEDA — performs the arithmetic against KEDA's metric.

## ScaledObject and ScaledJob

| Resource | Scales | Applicable when |
|---|---|---|
| `ScaledObject` | a Deployment, StatefulSet or scalable custom resource | long-running consumers processing a continuous stream |
| `ScaledJob` | Kubernetes `Job`s | discrete units of work with a natural end (batch, video encode) — one Job per item, no mid-flight interruption |

Where a single message can occupy a worker for minutes, a scale-down of a Deployment can terminate the pod mid-message. `ScaledJob` avoids that by spawning a Job per work item, whose completion is the unit of accounting rather than replica count.

## A ScaledObject on Kafka consumer lag

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: orders-consumer
spec:
  scaleTargetRef:
    name: orders-consumer        # the Deployment to scale
  minReplicaCount: 0             # scale to zero when idle
  maxReplicaCount: 50
  pollingInterval: 20            # seconds between trigger checks (default 30)
  cooldownPeriod: 120            # seconds idle before scaling back to 0
  triggers:
    - type: apache-kafka
      metadata:
        bootstrapServers: kafka.svc:9092
        consumerGroup: orders
        topic: orders
        lagThreshold: "50"          # target lag per replica → drives 1→N
        activationLagThreshold: "5" # lag needed to wake from 0→1
        offsetResetPolicy: latest
```

The two thresholds correspond to the two tracks of the state machine.

- **`activationLagThreshold`** is the operator's 0→1 switch. Below it — default `0` — the deployment remains at zero replicas. When measured lag crosses it, the operator scales the target to one and creates the conditions for the HPA to take over.
- **`lagThreshold`** — default `10` — is the HPA target value, expressed as desired lag *per replica*. With the deployment running, the HPA divides total consumer-group lag by this figure to derive a replica count: **500 messages of lag against a threshold of 50 yields a target of 10 pods**.

`pollingInterval` governs how often KEDA checks each trigger, default 30 seconds. At zero replicas it is the polling loop that notices the first message, so it bounds the delay before the 0→1 activation begins. `cooldownPeriod` is the interval the source must remain below the activation threshold before the operator deactivates the target back to zero. A short cooldown against a bursty source produces flapping: the deployment drops to zero during a lull and pays the cold start again on the next message.

## Other scalers

Only the trigger block changes per source. KEDA ships several dozen scalers; three recur:

- **RabbitMQ** (`type: rabbitmq`): `queueName`, a `mode` of `QueueLength` (message count) or `MessageRate`, and a `value` threshold, with `host` or `hostFromEnv` supplying the Advanced Message Queuing Protocol (AMQP) or management uniform resource identifier.
- **Prometheus** (`type: prometheus`): a `query` holding a Prometheus Query Language (PromQL) expression and a `threshold`. This is the general escape hatch — any load signal expressible as a Prometheus metric (in-flight jobs, 99th-percentile latency, a custom counter) becomes a scaling signal without a dedicated scaler.
- **Apache Kafka**: consumer-group lag, as above.

Credentials do not belong in the `ScaledObject`. A separate `TriggerAuthentication` or `ClusterTriggerAuthentication` resource references Kubernetes Secrets, workload identity, or environment values, leaving the scaling specification free of secrets.

## Pitfalls

- **Cold start on the first message after idle.** With `minReplicaCount: 0`, the message that crosses the activation threshold waits for pod scheduling, image pull and runtime warm-up before any processing begins; latency-sensitive paths therefore need `minReplicaCount: 1`.
- **Rebalance on every zero transition.** A consumer that participates in a Kafka consumer group triggers a **partition rebalance** when it leaves the group at scale-to-zero and again when it rejoins, stalling the whole group briefly each time.
- **Replicas beyond the partition count do no work.** Kafka assigns each partition to one consumer in a group, so `maxReplicaCount: 50` against a 12-partition topic leaves 38 pods idle irrespective of lag. KEDA scales the deployment; it cannot parallelise past what the source permits.
- **A short `cooldownPeriod` converts brief lulls into repeated cold starts.** The deployment deactivates during the gap and reactivates on the next arrival, paying scheduling and warm-up cost per lull rather than per burst.
- **Scaling a Deployment on a long unit of work risks mid-flight termination.** A scale-down chooses pods without regard to how far through a multi-minute message they are; that case calls for `ScaledJob`.
- **A malformed trigger is rejected only where the admission webhooks are installed.** Without them, an invalid `ScaledObject` is accepted by the API server and produces no scaling, which presents as a consumer that never wakes.
