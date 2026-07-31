---
title: "Health Checks and Graceful Shutdown: Draining a Pod Before It Dies"
date: 2026-07-31
track: microservices
summary: "Liveness, readiness, and startup probes do different jobs, and getting shutdown right means failing readiness and delaying SIGTERM so no request lands on a pod that is already leaving. Here is the exact Kubernetes sequence and the app-level code to match it."
reading_time: 5
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

Most "zero-downtime" deploys still drop a handful of requests on every rollout. The cause is almost always the same: a pod receives traffic for a moment after it has already decided to die. Fixing it means understanding that health checks and shutdown are one system, not two.

## Three probes, three questions

Kubernetes runs three kinds of probe, and they answer different questions. Newman frames health checks as a resiliency primitive in *Building Microservices*: a service must be able to say "do not send me work" independently of "I am broken." That distinction is exactly the liveness/readiness split.

- **Liveness** — "should this container be restarted?" If it fails `failureThreshold` times, the kubelet kills and restarts the container. Use it only for unrecoverable states (deadlock, wedged event loop). Never tie it to downstream dependencies, or one slow database takes out every replica in a restart storm.
- **Readiness** — "should this container receive traffic?" A failing readiness probe removes the pod from the Service's EndpointSlice, so kube-proxy and ingress stop routing to it. The container keeps running.
- **Startup** — "has this app finished booting?" It gates the other two so a slow JVM warmup does not trip liveness. Once it passes once, it never runs again.

Probe field defaults are worth memorizing because people set them by accident: `initialDelaySeconds: 0`, `periodSeconds: 10`, `timeoutSeconds: 1`, `successThreshold: 1`, `failureThreshold: 3`.

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

Keep `/livez` and `/readyz` as **separate endpoints**. `/livez` should return 200 as long as the process is sane. `/readyz` should reflect whether you want traffic right now — and that flag is what shutdown flips.

## The termination race

Here is what happens when a pod is deleted (rollout, scale-down, node drain). The `terminationGracePeriodSeconds` clock starts — **default 30 seconds** — and two things happen *concurrently*:

1. The kubelet runs your `preStop` hook, then sends **SIGTERM** to PID 1.
2. The pod is marked terminating, so the EndpointSlice controller removes it from the Service endpoints.

Step 2 is asynchronous. Endpoint removal has to propagate to every kube-proxy and every external load balancer before they stop routing — and as O'Leary notes, that update lands *after* the pod has already begun shutting down. So there is a window where SIGTERM has fired but traffic is still arriving at the old endpoint. If your process exits immediately on SIGTERM, those requests get a connection reset.

If the container is still alive when the grace period expires, the kubelet sends **SIGKILL**.

## Fixing it: fail readiness first, then wait

Two changes close the window.

**1. A `preStop` sleep** to keep the process serving while endpoint removal propagates. This is the single highest-leverage fix, because it works even if your app does nothing clever:

```yaml
lifecycle:
  preStop:
    exec:
      command: ["sh", "-c", "sleep 10"]
terminationGracePeriodSeconds: 45   # must exceed preStop + drain time
```

During those 10 seconds the pod is already removed from endpoints but still answers in-flight requests. Size the sleep to your slowest proxy's propagation, then add drain time on top.

**2. App-level draining on SIGTERM.** On receiving SIGTERM, flip `/readyz` to failing *first* (belt-and-suspenders against any proxy that still polls readiness), stop accepting new connections, let in-flight requests finish, then exit. In Go:

```go
func main() {
    srv := &http.Server{Addr: ":8080", Handler: mux}
    go srv.ListenAndServe()

    stop := make(chan os.Signal, 1)
    signal.Notify(stop, syscall.SIGTERM)
    <-stop

    ready.Store(false) // /readyz now returns 503

    ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
    defer cancel()
    srv.Shutdown(ctx) // stops accepting, drains in-flight, then returns
}
```

On the JVM the same shape applies: Spring Boot's `server.shutdown=graceful` with a `spring.lifecycle.timeout-per-shutdown-phase`, or a raw `Runtime.getRuntime().addShutdownHook` that stops the acceptor and awaits in-flight work. The ordering is the invariant, not the language: **readiness fails before the socket closes, and the process outlives endpoint propagation.**

One ordering trap: your drain timeout in `Shutdown(ctx)` plus the `preStop` sleep must fit inside `terminationGracePeriodSeconds`, or SIGKILL cuts you off mid-drain. Here 10s sleep + 30s drain = 40s < 45s grace. Leave headroom.

## Verifying it

Run a load generator against the Service and trigger a rollout:

```sh
hey -z 60s -c 50 http://my-svc.default.svc.cluster.local/ &
kubectl rollout restart deployment/my-svc
```

With the pattern in place you should see zero non-200s across the restart. Remove the `preStop` sleep and rerun — the reappearing connection resets are the exact requests that were landing on dying pods.

**Try next:** Add a `preStop: sleep 10` hook and separate `/livez` and `/readyz` endpoints to one service, then measure error count during `kubectl rollout restart` with and without the hook to see the window close.
