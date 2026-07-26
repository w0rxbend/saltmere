---
title: "Replicated Load-Balanced Services: The Base Case of Scaling"
date: 2026-07-26
track: sys-patterns
summary: "Before sharding, leader election, or work queues, there's the pattern everything else deviates from: N identical stateless replicas behind a load balancer. A close read of Burns' replicated load-balanced service pattern, why readiness and liveness are different questions, and a full Deployment/Service/HPA to run it."
reading_time: 5
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

Every other pattern in this journal is a deviation from one base case. Sidecars attach helpers to a pod. Ambassadors proxy out. Sharded services split state across replicas that stop being interchangeable. Work queues hand out tasks instead of requests. But underneath all of them sits the pattern Burns opens *Designing Distributed Systems* with: the **replicated load-balanced service** — N identical copies of a stateless process behind a load balancer, scaled by adding or removing copies. It's the least interesting pattern in the book and the most load-bearing.

## The pattern: clone it, balance it, forget which one answered

The shape is almost embarrassingly simple. A load balancer sits in front of a pool of replicas. Every replica runs the same code, holds no client-specific state, and can answer any request from any client. The load balancer's only job is to pick a replica — round-robin, least-connections, whatever — and it never needs to remember which replica handled the last request from a given client, because it wouldn't matter if it did.

That indifference is the entire value proposition. Because any replica can serve any request:

- You scale by changing a replica count, not by re-architecting anything.
- A replica crashing is a capacity dip, not a data-loss event or an outage for a subset of users.
- Rolling deploys work: replace replicas one at a time, and clients never notice which version answered.

Burns frames this as the foundational serving pattern precisely because it requires no coordination protocol between replicas — no consensus, no leader, no gossip. The load balancer is the only component that needs to know the pool exists.

## Statelessness is the enabling constraint

None of the above holds if a replica remembers something a later request depends on. If replica B doesn't have the shopping cart replica A built up, routing request 2 to B breaks the illusion of one service. So statelessness isn't a nice property this pattern happens to have — it's the constraint that makes the pattern legal at all. Everything else (readiness gating, autoscaling, indifferent load balancing) is a consequence of having satisfied it first.

In practice "stateless" means: no data that must survive past the response is kept in process memory or on local disk. State that must persist goes somewhere shared and addressable — a database, an object store, a distributed cache — that every replica can reach identically. The replica itself becomes disposable: kill it, restart it, replace its image, and nothing downstream notices except a momentary capacity dip.

## Readiness vs. liveness: why the load balancer must not route blindly

A pool of identical replicas is only safe to load-balance across if the load balancer actually knows which ones are fit to receive traffic *right now* — not just which ones are running. Kubernetes splits this into two separate questions, and conflating them is the most common way to break this pattern in production:

| Probe | Question it answers | Failure action | Wrong answer costs you |
|---|---|---|---|
| **Liveness** | Is the process alive, or wedged in a way only a restart fixes? | kubelet kills and restarts the container | A stuck-but-not-crashed process serves errors forever |
| **Readiness** | Can this replica handle traffic *right now*? | Pod is removed from Service endpoints — no restart | A cold-starting or overloaded replica gets requests it can't serve |

A replica that's warming a cache, waiting on a downstream dependency, or momentarily saturated is *alive* — restarting it would be pointless, even harmful — but it isn't *ready*. The Kubernetes docs are explicit that failing a readiness probe only pulls the pod's IP from the Service's endpoint list; it doesn't touch the container. Failing a liveness probe does the opposite: the kubelet restarts the container regardless of load-balancer membership. Point the load balancer at readiness, not liveness, and a pod stuck initializing never receives a request it would fail; point restarts at liveness, and a genuinely wedged process gets recycled instead of silently eating traffic.

## Where session and caching state actually live

Statelessness doesn't mean the system has no state — it means the *replica* doesn't own it. Two flavors show up constantly in this pattern:

- **Session state** — instead of sticky sessions pinning a client to one replica (which quietly reintroduces the coupling this pattern exists to avoid), session data goes into a shared store like Redis, keyed by a session token the client presents on every request. Any replica reads it, so any replica can serve that client.
- **Caching** — a per-replica in-process cache is fine as a pure performance optimization *as long as a cache miss is harmless and just costs a slower path to the shared source of truth*. The moment correctness depends on which replica's cache you hit, you've smuggled state ownership back into the replica.

Both patterns keep the load balancer's job trivial: pick any ready replica, because "any" really does mean any.

## Horizontal autoscaling: replica count as a dial

Because replicas are identical and disposable, capacity becomes a single tunable number instead of an architecture decision. A `HorizontalPodAutoscaler` watches a metric — typically CPU or memory utilization averaged across the pool — against a target, and adjusts `replicas` up or down within bounds. This only works cleanly because statelessness already guaranteed that adding replica N+1 requires no handoff, migration, or rebalancing: the new pod becomes ready, the load balancer includes it, done.

## Concrete: Deployment, Service, and HPA together

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

The `Service` is the load balancer: it only ever forwards to pods currently listed as ready endpoints. The two separate probe paths — `/healthz/ready` versus `/healthz/live` — exist because they answer different questions, per the table above. The HPA turns `replicas` into a live variable driven by observed CPU load, with `minReplicas: 3` as a floor for baseline availability and `maxReplicas: 20` as a ceiling against runaway scale-out.

## When replicas aren't enough

This pattern scales exactly one axis: how many identical copies of the same small unit of state-free work you're willing to run. It stops working the moment the thing being served is itself too large for any one replica to hold or compute alone — a cache with more keys than fit in memory, an index too big for one disk. That's the sharded-service pattern's territory, covered separately in this track: same load-balancer instinct, but the router picks the *one correct* shard instead of *any* ready replica. Reach for sharding only after you've confirmed the bottleneck is data volume, not request volume — replicated services solve request volume for free.

**Try next:** deploy the manifest above, then `kubectl exec` into a pod and make `/healthz/ready` return 500 without touching `/healthz/live` — watch the pod stay running but drop out of `kubectl get endpoints catalog-api` within one probe interval, and confirm no in-flight liveness restart happens because the process itself never stopped answering.
