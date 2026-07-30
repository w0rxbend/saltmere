---
title: "KEDA: scaling a consumer on queue depth — including down to zero"
date: 2026-07-30
track: sys-patterns
summary: "KEDA is the event-driven serving/scaling pattern made real: it drives the Kubernetes HPA off queue depth and event-source metrics, and — unlike the stock HPA on external metrics alone — it scales your consumer all the way to zero and back."
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

Brendan Burns' *Designing Distributed Systems* splits scaling into two ideas: **replicated, load-balanced serving** (put N identical stateless replicas behind a balancer) and **scaling on the right signal**. For request-driven services the signal is CPU or requests-per-second, and Kubernetes' Horizontal Pod Autoscaler (HPA) handles it out of the box. But a queue consumer isn't request-driven. Its load isn't CPU — it's *backlog*. Ten thousand unprocessed Kafka messages should mean "add pods" even if every existing pod is idle waiting on a slow downstream. And when the queue is empty, the right replica count is often **zero**.

The stock HPA can't express either half of that cleanly. It scales on external metrics only via a metrics adapter you have to build and wire yourself, and it will not scale a workload to zero on those metrics. [KEDA](https://keda.sh/docs/latest/concepts/) — Kubernetes Event-Driven Autoscaling — is the productionized version of Burns' event-driven scaling pattern that fills exactly that gap. It's a CNCF **graduated** project (graduated [August 22, 2023](https://www.cncf.io/announcements/2023/08/22/cloud-native-computing-foundation-announces-graduation-of-kubernetes-autoscaler-keda/)), and the current release is **v2.20.1** (June 2026), with the docs' `latest` tracking the 2.20 line.

## What KEDA actually is

KEDA describes itself as a lightweight *complement* to Kubernetes, not a replacement. It installs three pieces:

- **keda-operator** (the "agent") — watches your `ScaledObject`/`ScaledJob` resources and manages the HPA lifecycle. Crucially, it handles the **0↔1 transition directly**: it activates a scaled-to-zero deployment when events arrive, and deactivates it back to zero when the source goes idle.
- **keda-metrics-apiserver** — a Kubernetes external-metrics adapter. It polls the event source (Kafka, RabbitMQ, Prometheus, SQS, ...) and exposes those numbers to the HPA through the standard external metrics API.
- **keda-admission-webhooks** — validates each resource at apply time, so a malformed trigger fails fast instead of silently doing nothing.

The division of labor is the key insight: **KEDA owns 0→1 and 1→0; the HPA owns 1→N.** KEDA never fights the HPA — it *feeds* it. Once at least one replica is running, a perfectly ordinary HPA (which KEDA created and owns) does the arithmetic against KEDA's metric.

## ScaledObject vs ScaledJob

Two resources, two shapes of work:

| Resource | Scales | Use when |
|---|---|---|
| `ScaledObject` | a Deployment / StatefulSet / scalable CRD | long-running consumers that process a continuous stream |
| `ScaledJob` | Kubernetes `Job`s | discrete, long-running units of work with a natural end (batch, video encode) — one Job per item, no mid-flight interruption |

If a single message can take minutes and must not be killed by a scale-down, reach for `ScaledJob`: KEDA spawns Jobs rather than churning replicas of a Deployment. For a steady-state stream consumer, `ScaledObject` is what you want.

## A concrete ScaledObject: Kafka consumer lag

Here's the whole pattern in one file — a Kafka consumer that lives at zero replicas until there's lag, scales out on backlog, and drains back to zero:

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
  pollingInterval: 20            # seconds between metric checks (while active)
  cooldownPeriod: 120           # seconds idle before scaling back to 0
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

Two thresholds, two jobs, matching the two-track model:

- **`activationLagThreshold`** is the operator's 0→1 switch. Below it (default `0`), the deployment stays at zero. Cross it, and KEDA brings up the first pod.
- **`lagThreshold`** (default `10`) is the HPA target — the desired lag *per replica*. Once running, the HPA divides total consumer-group lag by this to pick a replica count. At 500 messages of lag and a threshold of 50, it aims for 10 pods.

`pollingInterval` sets how often KEDA queries Kafka; `cooldownPeriod` is how long the source must stay quiet before KEDA deactivates back to zero. Set `cooldownPeriod` generously — flapping to zero on a brief lull just pays cold-start cost twice.

## Other scalers, same shape

The trigger block is the only thing that changes per source. KEDA ships 70+ scalers; three common ones:

- **RabbitMQ** (`type: rabbitmq`): `queueName`, a `mode` of `QueueLength` (message count) or `MessageRate`, and a `value` threshold, with `host`/`hostFromEnv` for the AMQP or management URI. Scale on how deep the queue is.
- **Prometheus** (`type: prometheus`): give it a `query` (a PromQL expression) and a `threshold`. This is the escape hatch — if you can express your load signal as a Prometheus metric (in-flight jobs, p99 latency, custom counters), you can scale on it without a dedicated scaler.
- **Apache Kafka** (above): consumer-group lag, the canonical stream case.

Secrets don't go in the `ScaledObject`. A separate `TriggerAuthentication` (or `ClusterTriggerAuthentication`) resource references Kubernetes Secrets, workload identity, or environment values, keeping the scaling spec free of credentials.

## Where it bites

Scale-to-zero isn't free. The first message after idle pays a **cold start** — pod schedule, image pull, JVM/runtime warm-up — so latency-sensitive paths may want `minReplicaCount: 1`. If your consumer participates in a Kafka consumer group, dropping to zero and back triggers a **partition rebalance**, briefly stalling the group. And the number of *useful* replicas is capped by partition count: 50 pods on a 12-partition topic leaves 38 idle regardless of lag. KEDA scales the deployment; it can't parallelize past what the source allows.

Used with those constraints in mind, KEDA turns Burns' event-driven scaling pattern from a whiteboard diagram into ~20 lines of YAML — and gives you the one thing the raw HPA won't: a consumer that costs nothing when there's nothing to consume.

**Try next:** deploy a dummy consumer with `minReplicaCount: 0` and a RabbitMQ or Kafka trigger, then `kubectl get hpa,scaledobject -w` while you publish a burst — watch KEDA create the HPA, wake the deployment from zero, scale out on backlog, and drain back down.
