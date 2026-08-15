---
title: "Health Checks and Graceful Shutdown: Draining a Pod Before It Dies"
date: 2026-07-31
track: microservices
summary: "Liveness, readiness and startup probes answer different questions, and correct shutdown requires failing readiness and delaying process exit so that no request lands on a pod that has already begun terminating. The exact Kubernetes termination sequence and the matching application-level ordering."
reading_time: 6
tags: [kubernetes, microservices, resiliency, graceful-shutdown, health-checks, sigterm]
sources:
  - title: "Kubernetes: Liveness, Readiness and Startup Probes"
    url: "https://kubernetes.io/docs/concepts/workloads/pods/probes/"
  - title: "Kubernetes: Configure Probes (field defaults)"
    url: "https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/"
  - title: "Kubernetes: Pod Lifecycle (termination)"
    url: "https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/"
  - title: "Making K8s shutdowns more graceful (Michael O'Leary)"
    url: "https://michaeloleary.net/kubernetes/graceful-k8s-shutdowns-for-better-load-balancing/"
  - title: "Sam Newman, Building Microservices, 2nd Edition"
    url: "https://samnewman.io/books/building_microservices_2nd_edition/"
---

**Gist.** Deployments advertised as zero-downtime still drop requests on every rollout, because a pod continues to receive traffic for a short interval after it has been told to terminate. The remedy is to treat health checks and shutdown as one mechanism: readiness fails first, the process keeps serving until endpoint removal has propagated to every proxy, and only then does the listening socket close. The cost is a deliberately slower termination — every pod deletion now takes the propagation delay plus the drain window, and that sum must fit inside `terminationGracePeriodSeconds`.

## Three probes, three questions

Kubernetes runs three kinds of probe, and they answer different questions. Newman treats health checks as a resiliency primitive in *Building Microservices*, arguing that a binary healthy/unhealthy verdict is too coarse for routing decisions. Kubernetes draws the corresponding line as the liveness/readiness split: an instance that should not be sent work is a different state from an instance that is broken.

- **Liveness** — "should this container be restarted?" After `failureThreshold` consecutive failures the kubelet kills and restarts the container. It is appropriate only for unrecoverable states such as a deadlock or a wedged event loop. Tying it to downstream dependencies couples every replica to one slow database, and a shared failure then produces a **correlated restart storm** rather than a degraded but running fleet.
- **Readiness** — "should this container receive traffic?" A failing readiness probe removes the pod from the Service's EndpointSlice, so kube-proxy and ingress controllers stop routing to it. The container itself keeps running, so recovery requires no restart.
- **Startup** — "has this application finished booting?" It gates the other two, so a slow Java Virtual Machine (JVM) warm-up does not trip liveness. **Once it succeeds, it does not run again** for the lifetime of the container.

The probe field defaults are load-bearing because they apply whenever a field is omitted: `initialDelaySeconds: 0`, `periodSeconds: 10`, `timeoutSeconds: 1`, `successThreshold: 1`, `failureThreshold: 3`. A `timeoutSeconds` of 1 means a handler that occasionally takes longer than one second is recorded as a failure regardless of the response it eventually produces.

```yaml
livenessProbe:
  httpGet: { path: /livez, port: 8080 }
  periodSeconds: 10
  failureThreshold: 3          # ~30s of failure before restart
readinessProbe:
  httpGet: { path: /readyz, port: 8080 }
  periodSeconds: 5
  failureThreshold: 2
startupProbe:
  httpGet: { path: /livez, port: 8080 }
  periodSeconds: 5
  failureThreshold: 30         # allow up to ~150s to boot
```

`/livez` and `/readyz` must be **separate endpoints**. `/livez` returns 200 for as long as the process is internally sound; `/readyz` reflects whether the instance should be sent traffic at this instant. That second flag is the one shutdown flips.

## The termination race

When a pod is deleted — by a rollout, a scale-down or a node drain — the `terminationGracePeriodSeconds` clock starts, **defaulting to 30 seconds**, and two sequences proceed *concurrently*:

1. The kubelet runs the `preStop` hook, and on its completion sends **SIGTERM** to process ID 1 in the container.
2. The pod is marked terminating, and the EndpointSlice controller removes it from the Service endpoints.

The second sequence is asynchronous with respect to the first. Endpoint removal has to reach every kube-proxy instance and every external load balancer before those components stop routing, and as O'Leary describes, that update lands *after* the pod has already begun shutting down. The result is a window in which SIGTERM has been delivered but new connections are still arriving at the endpoint. **A process that exits immediately on SIGTERM answers those connections with a reset**, which is the observed error during an otherwise healthy rollout.

If the container is still running when the grace period expires, the kubelet sends **SIGKILL**, which is not catchable and terminates in-flight work.

## Closing the window: fail readiness, then wait

Two changes close it.

**A `preStop` sleep** keeps the process serving while endpoint removal propagates. It requires no application change, which makes it applicable to containers whose code cannot be modified:

```yaml
lifecycle:
  preStop:
    exec:
      command: ["sh", "-c", "sleep 10"]
terminationGracePeriodSeconds: 45   # must exceed preStop + drain time
```

During those 10 seconds the pod has been removed from the endpoints but continues answering requests. The sleep is sized to the propagation delay of the slowest proxy in the path, with the drain time added on top.

**Application-level draining on SIGTERM** supplies the second half. On receipt of the signal the handler flips `/readyz` to failing *first*, which covers any proxy that polls readiness directly rather than watching EndpointSlices; then it stops accepting new connections, allows in-flight requests to complete, and exits. The ordering is the invariant and it is independent of language: **readiness fails before the listening socket closes, and the process outlives endpoint propagation.**

The arithmetic constraint is that the drain timeout plus the `preStop` sleep must fit inside `terminationGracePeriodSeconds`, otherwise SIGKILL interrupts the drain. In the configuration above, a 10-second sleep and a 30-second drain total 40 seconds against a 45-second grace period.

### Implementation sketch (Scala)

The JDK delivers SIGTERM to registered shutdown hooks, and `com.sun.net.httpserver.HttpServer.stop(delay)` stops accepting new exchanges while giving open ones up to `delay` seconds to finish — the two primitives the ordering needs.

```scala
import com.sun.net.httpserver.{HttpExchange, HttpServer}
import java.net.InetSocketAddress
import java.util.concurrent.atomic.AtomicBoolean

val ready = AtomicBoolean(true)

def respond(ex: HttpExchange, code: Int): Unit =
  ex.sendResponseHeaders(code, -1)
  ex.close()

val server = HttpServer.create(InetSocketAddress(8080), 0)

// /livez reports process sanity only: it must stay 200 during draining,
// otherwise the kubelet restarts a container that is shutting down correctly.
server.createContext("/livez", ex => respond(ex, 200))
server.createContext("/readyz", ex => respond(ex, if ready.get() then 200 else 503))
server.start()

Runtime.getRuntime.addShutdownHook(Thread { () =>
  ready.set(false)              // step 1: stop being an eligible endpoint
  Thread.sleep(5_000)           // step 2: in-process stand-in for the preStop sleep;
                                // omit it when preStop already supplies the delay
  server.stop(30)               // step 3: refuse new exchanges, drain open ones
})
```

The same shape holds for Spring Boot's `server.shutdown=graceful` together with `spring.lifecycle.timeout-per-shutdown-phase`: the framework performs steps 1 and 3, and the `preStop` hook or an in-hook sleep supplies step 2.

## Verifying it

A load generator run against the Service across a rollout exposes the window directly. The generator has to sit inside the cluster, since the cluster-local name is what the in-cluster proxy path resolves:

```sh
# in a pod on the same cluster
hey -z 60s -c 50 http://my-svc.default.svc.cluster.local/ &
# from an admin shell, while the load is running
kubectl rollout restart deployment/my-svc
```

The measurement of interest is the difference between two runs rather than an absolute figure: removing the `preStop` sleep and repeating the run reintroduces connection resets, and their count approximates the number of requests that landed on pods already past the start of termination. No single error count is portable, because the propagation delay depends on the proxies in the path.

## Pitfalls

- **Liveness probing a downstream dependency.** A database outage fails the probe on every replica at once, the kubelet restarts them all, and the restart storm removes capacity that the outage alone would have left intact.
- **One endpoint serving both probes.** When `/healthz` backs liveness and readiness, marking the instance not-ready during drain also fails liveness, and the kubelet restarts a container that is shutting down correctly.
- **`preStop` sleep plus drain timeout exceeding the grace period.** SIGKILL arrives mid-drain, so in-flight requests are cut and the observed error rate is unchanged by the fix.
- **Exiting immediately on SIGTERM.** Endpoint removal has not yet reached every proxy, so connections arriving in that window are met by a closed socket and reset.
- **Relying on the default `timeoutSeconds: 1`.** A readiness handler that touches a slow dependency intermittently exceeds one second, the pod is pulled from the EndpointSlice, and traffic oscillates without the process ever being unhealthy.
- **A startup probe whose `failureThreshold × periodSeconds` is shorter than the real boot time.** The container is killed and restarted before it finishes initializing, producing a crash loop that resembles an application fault.
