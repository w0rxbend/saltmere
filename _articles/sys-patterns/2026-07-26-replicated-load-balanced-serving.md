---
title: "Replicated Load-Balanced Services: The Base Case of Scaling"
date: 2026-07-26
track: sys-patterns
summary: "Before sharding, leader election, or work queues comes the pattern the others deviate from: N identical stateless replicas behind a load balancer. A close read of Burns' replicated load-balanced service pattern, the separation of readiness from liveness, and a Deployment/Service/HorizontalPodAutoscaler manifest that runs it."
reading_time: 7
tags: [kubernetes, statelessness, load-balancing, autoscaling, health-checks, burns]
sources:
  - title: "Designing Distributed Systems, 2nd ed. — Ch. 6, Replicated Load-Balanced Services (Burns, O'Reilly)"
    url: "https://www.oreilly.com/library/view/designing-distributed-systems/9781098156343/ch06.html"
  - title: "designing-distributed-systems-labs — 2.1 Replicated Load Balanced Services (Brendan Burns, GitHub)"
    url: "https://github.com/brendandburns/designing-distributed-systems-labs/blob/master/2.%20Serving%20Patterns/2.1.%20Replicated%20Load%20Balanced%20Services/README.md"
  - title: "Liveness, Readiness, and Startup Probes — Kubernetes docs"
    url: "https://kubernetes.io/docs/concepts/workloads/pods/probes/"
  - title: "Configure Liveness, Readiness and Startup Probes — Kubernetes docs"
    url: "https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/"
  - title: "HorizontalPodAutoscaler Walkthrough — Kubernetes docs"
    url: "https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale-walkthrough"
---

**Gist.** Request volume grows faster than any single process can absorb it. The replicated load-balanced service answers with **N identical copies of a stateless process behind a load balancer**, so capacity becomes a replica count rather than an architectural decision. The cost is the constraint that buys the indifference: **no replica may own state that a later request depends on**, which pushes session data, caches of record, and anything durable into a shared store reachable identically from every replica.

Every other serving pattern in this track is a deviation from this base case. Sidecars attach helpers to a pod; ambassadors proxy outbound; sharded services split state across replicas that stop being interchangeable; work queues hand out tasks rather than requests. Burns presents the replicated load-balanced service as the first of the serving patterns in *Designing Distributed Systems*, and describes it as the simplest of them.

## The invariant: any ready replica can answer any request

A load balancer sits in front of a pool of replicas. Every replica runs the same code, holds no client-specific state, and can answer any request from any client. The balancer's only responsibility is selecting a replica — round-robin, least-connections, or another policy — and it need not record which replica served a given client previously, because that record would carry no information.

The invariant is **request-to-replica affinity is unnecessary for correctness**. Three consequences follow directly, and each fails if the invariant fails:

- Capacity scales by changing a replica count; no rebalancing, handoff, or migration accompanies the change.
- A replica crash is a capacity reduction, not data loss and not an outage confined to a subset of clients.
- Rolling deployment is a matter of replacing replicas one at a time, since a client cannot observe which version answered.

The pattern requires **no coordination protocol between replicas** — no consensus, no leader election, no gossip. The load balancer is the sole component that must know the pool's membership.

## Statelessness is the enabling constraint, not a side effect

If replica B lacks a shopping cart accumulated on replica A, routing the second request to B breaks the illusion of a single service. Statelessness is therefore the precondition that makes the pattern legal, and readiness gating, autoscaling, and affinity-free balancing are consequences of having satisfied it.

Operationally, stateless means **no data that must survive past the response is held in process memory or on local disk**. State that must persist moves to a shared, addressable store — a database, an object store, a distributed cache — that every replica reaches identically. The replica becomes disposable: it may be killed, restarted, or replaced with a new image, and the only downstream effect is a capacity reduction until the replacement becomes ready.

Two flavours of state recur in this pattern and are worth separating:

- **Session state.** Sticky sessions pin a client to one replica and reintroduce exactly the coupling the pattern exists to remove. The alternative is a shared store such as Redis keyed by a session token the client presents on each request, so that any replica can read it.
- **Caching.** A per-replica in-process cache remains admissible **as a pure performance optimisation, provided a miss is harmless and costs only the slower path to the shared source of truth**. Once correctness depends on which replica's cache was hit, state ownership has re-entered the replica and the invariant no longer holds.

## Readiness and liveness answer different questions

Balancing across identical replicas is safe only when the balancer knows which replicas are fit to receive traffic *at that moment*, which is not the same set as those that are running. Kubernetes separates the two questions, and conflating them is a common way to break the pattern in production.

| Probe | Question answered | Failure action | Cost of a wrong answer |
|---|---|---|---|
| **Liveness** | Is the process alive, or wedged such that only a restart recovers it? | The kubelet kills and restarts the container | A stuck-but-not-crashed process serves errors indefinitely |
| **Readiness** | Can this replica handle traffic right now? | The pod is removed from the Service's endpoints; no restart | A cold-starting or saturated replica receives requests it cannot serve |

A replica warming a cache, waiting on a downstream dependency, or momentarily saturated is alive but not ready; restarting it would discard the warm-up work already done. The Kubernetes documentation states that **failing a readiness probe removes the pod's address from the Service's endpoint list and leaves the container untouched**, whereas **failing a liveness probe causes the kubelet to restart the container**, independent of load-balancer membership. Pointing the balancer at readiness keeps an initialising pod from receiving a request it would fail; pointing restarts at liveness recycles a genuinely wedged process rather than leaving it to absorb traffic.

### Implementation sketch (Scala)

The load-bearing idea is that the two endpoints read **different** variables: liveness reflects only whether the serving loop is still progressing, while readiness additionally requires warm-up completion and a drain flag that shutdown sets before the process stops accepting work.

```scala
final class HealthState:
  private val warmedUp   = java.util.concurrent.atomic.AtomicBoolean(false)
  private val draining   = java.util.concurrent.atomic.AtomicBoolean(false)
  private val lastLoopAt = java.util.concurrent.atomic.AtomicLong(System.nanoTime())

  def markWarm(): Unit  = warmedUp.set(true)
  def beginDrain(): Unit = draining.set(true)          // called from the SIGTERM handler
  def heartbeat(): Unit = lastLoopAt.set(System.nanoTime())

  /** Liveness: only a restart can fix a serving loop that has stopped advancing. */
  def live(stallLimit: java.time.Duration): Boolean =
    System.nanoTime() - lastLoopAt.get() < stallLimit.toNanos

  /** Readiness: alive, warm, and not draining. Failing this removes the pod
    * from Service endpoints without restarting the container. */
  def ready(stallLimit: java.time.Duration): Boolean =
    live(stallLimit) && warmedUp.get() && !draining.get()

// Handler wiring, framework-agnostic:
val health = HealthState()
val stall  = java.time.Duration.ofSeconds(30)

def handle(path: String): Int = path match
  case "/healthz/live"  => if health.live(stall) then 200 else 500
  case "/healthz/ready" => if health.ready(stall) then 200 else 503
  case _                => 404
```

`beginDrain` is the piece that makes rolling deployment lossless: the pod reports not-ready one probe interval before it stops serving, so the endpoint controller withdraws it while in-flight requests finish.

## Horizontal autoscaling turns the replica count into a dial

Because replicas are identical and disposable, capacity reduces to a single tunable number. A `HorizontalPodAutoscaler` compares a metric — commonly CPU or memory utilisation averaged across the pool — against a target and adjusts `replicas` within configured bounds. This is only sound because statelessness already guarantees that adding replica N+1 requires no handoff, migration, or rebalancing: the new pod becomes ready and the load balancer includes it.

## Deployment, Service, and HorizontalPodAutoscaler together

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: catalog-api
spec:
  replicas: 3
  selector:
    matchLabels: { app: catalog-api }
  template:
    metadata:
      labels: { app: catalog-api }
    spec:
      containers:
        - name: catalog-api
          image: registry.example.com/catalog-api:2.3.0
          ports:
            - containerPort: 8080
          resources:
            requests: { cpu: "250m", memory: "256Mi" }
            limits: { cpu: "500m", memory: "512Mi" }
          readinessProbe:
            httpGet: { path: /healthz/ready, port: 8080 }
            initialDelaySeconds: 5
            periodSeconds: 5
            failureThreshold: 3
          livenessProbe:
            httpGet: { path: /healthz/live, port: 8080 }
            initialDelaySeconds: 15
            periodSeconds: 10
            failureThreshold: 3
---
apiVersion: v1
kind: Service
metadata:
  name: catalog-api
spec:
  selector: { app: catalog-api }
  ports:
    - port: 80
      targetPort: 8080
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: catalog-api
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: catalog-api
  minReplicas: 3
  maxReplicas: 20
  metrics:
    - type: Resource
      resource:
        name: cpu
        target: { type: Utilization, averageUtilization: 70 }
```

The `Service` is the load balancer: it forwards only to pods currently listed as ready endpoints. The two probe paths differ because they answer the two questions tabulated above. The autoscaler makes `replicas` a variable driven by observed CPU utilisation, with `minReplicas: 3` as a floor and `maxReplicas: 20` as a ceiling on scale-out.

## The axis this pattern does not scale

The pattern scales one axis: the number of identical copies of a state-free unit of work. It stops applying when the served thing is itself too large for one replica to hold or compute — a cache with more keys than fit in memory, an index larger than one disk. That is the sharded-service pattern's territory, covered separately in this track: the same balancing instinct, but the router selects the *one correct* shard rather than *any* ready replica. Sharding is warranted once the bottleneck is confirmed to be data volume rather than request volume, since replication already addresses request volume without a routing protocol.

An instructive experiment on the manifest above: make `/healthz/ready` return 503 while `/healthz/live` continues to return 200, then observe the pod remain `Running` while disappearing from `kubectl get endpoints catalog-api`, with no restart, because the process never stopped answering the liveness path.

## Pitfalls

- **A readiness probe that reports a downstream dependency's health takes the whole pool out of rotation at once.** When the shared database is briefly unreachable, every replica fails readiness simultaneously, the Service has zero endpoints, and requests fail at the balancer rather than degrading per-replica.
- **A liveness probe on the same handler as readiness converts overload into a restart storm.** A saturated process that cannot answer within the probe timeout is killed, its load shifts to the remaining replicas, and they saturate in turn.
- **Sticky sessions added to work around per-replica state make crashes user-visible again.** Losing a replica now loses the sessions pinned to it, which is the failure mode the pattern was adopted to remove.
- **An in-process cache treated as authoritative produces responses that depend on which replica answered.** The symptom is a client failing to observe its own recent write, appearing only under multi-replica deployment and vanishing at `replicas: 1`.
- **`initialDelaySeconds` shorter than actual start-up causes restart loops before readiness ever succeeds.** The liveness probe fires during warm-up, the container restarts, and warm-up begins again.
- **Terminating pods that do not report not-ready before exiting drop in-flight requests.** The endpoint controller withdraws the address only after the pod's state changes, so a process exiting immediately on SIGTERM leaves the balancer forwarding to a closed socket.
- **An autoscaler targeting CPU on a latency-bound workload does not scale under load.** Replicas blocked on a downstream call show low CPU utilisation while queueing, so the target is never exceeded and the replica count stays at the floor.
