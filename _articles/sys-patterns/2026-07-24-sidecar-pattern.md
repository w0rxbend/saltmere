---
title: "The sidecar pattern: adding capabilities to a service without modifying its code"
date: 2026-07-24
track: sys-patterns
summary: "A sidecar is a second container scheduled alongside an application in the same pod, sharing its network namespace and, optionally, its volumes. It supplies TLS termination, log shipping or proxying to a service whose source cannot be changed."
reading_time: 6
tags: [sidecar, kubernetes, containers, patterns, burns]
sources:
  - title: "Brendan Burns, Designing Distributed Systems (2nd ed.) — Single-Node Patterns"
    url: "https://www.oreilly.com/library/view/designing-distributed-systems/9781098156343/"
  - title: "Kubernetes: Sidecar Containers"
    url: "https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/"
---

**Gist.** Cross-cutting capabilities — transport security, log collection, configuration reload, caching — are needed by an application whose code cannot be modified, either because it is third-party or because the change would couple unrelated concerns into one release unit. The sidecar pattern, the first of the single-node patterns in Burns' *Designing Distributed Systems*, places a second container in the same scheduling unit (a Kubernetes pod) so that it observes the application's `localhost` traffic and, where a volume is shared, its files, while shipping as an independent image. The cost is that the two containers are fused for their whole lifetime: they are co-scheduled onto one node, they scale together at exactly 1:1, and they draw from the same node's resource budget.

## The mechanism: what a pod shares

A pod is not merely a naming convention for containers. It is the unit of scheduling and of namespace sharing, and the sidecar pattern is a direct consequence of three properties.

**All containers in a pod share one network namespace.** They have a single pod IP address and a single port space. A process in the sidecar reaches the application at `localhost:8080` with no service discovery, no DNS resolution and no traversal of the cluster network. The corollary is a constraint rather than a convenience: **two containers in the same pod cannot bind the same port**, because there is one namespace and therefore one set of listening sockets.

**Filesystems are not shared by default.** Each container has its own root filesystem from its own image. Sharing happens only where a volume declared at the pod level is mounted into both containers. A log-shipping sidecar reads the application's log directory because that directory is an `emptyDir` volume mounted into both, not because containers in a pod see each other's disks.

**The process namespace is not shared by default either.** A sidecar cannot signal or inspect the application's processes unless the pod sets `shareProcessNamespace`. Config-reload sidecars that work by sending a signal to the main process depend on that flag; those that work by rewriting a file on a shared volume and letting the application watch it do not.

The result is a boundary that is sharp in the dimensions that matter for ownership — separate image, separate version, separate resource requests and limits, separate crash and restart accounting — and deliberately porous in the dimension that matters for function, the network.

## A concrete instance: HTTPS for a plain-HTTP application

Consider a service that speaks only Hypertext Transfer Protocol (HTTP) on port 8080 and whose source is unavailable. An nginx sidecar terminates Transport Layer Security (TLS) and proxies to `localhost:8080`:

```yaml
apiVersion: v1
kind: Pod
metadata: { name: legacy-app }
spec:
  containers:
    - name: app                     # unchanged, HTTP-only
      image: legacy-app:1.4
      ports: [{ containerPort: 8080 }]
    - name: tls-sidecar             # same network namespace as app
      image: nginx:stable
      ports: [{ containerPort: 443 }]
      volumeMounts:
        - { name: certs, mountPath: /etc/nginx/certs, readOnly: true }
        - { name: conf,  mountPath: /etc/nginx/conf.d, readOnly: true }
  volumes:
    - { name: certs, secret: { secretName: app-tls } }
    - { name: conf,  configMap: { name: nginx-proxy-conf } }
```

The referenced ConfigMap contains a `listen 443 ssl;` server block whose location does `proxy_pass http://localhost:8080;`. The application acquires no knowledge of TLS. Certificate rotation, an nginx upgrade and the addition of rate limiting are all changes to the sidecar's image or ConfigMap, and none of them produce a new build of `legacy-app`.

One property of this arrangement is easy to overlook and load-bearing for security: **binding the application to 8080 does not hide it**. Any container in the pod, and anything that can route to the pod IP address on 8080, still reaches plaintext HTTP. The sidecar adds an encrypted path; it does not remove the unencrypted one. Closing that gap requires the application to listen on the loopback interface only, or a network policy that admits traffic on 443 alone.

## Why the pattern generalises

A service mesh — Istio, Linkerd — is a proxy sidecar injected into every pod, intercepting the pod's inbound and outbound traffic and applying mutual TLS, retries and telemetry without the application's participation. Log shippers, secret-rotation agents and configuration-sync daemons are the same construction with a different payload. Two named variations differ only in the direction and shape of the interposition: the **ambassador** proxies the application's *outbound* calls, and the **adapter** normalises the interface the application *exposes* — for instance, translating an application's idiosyncratic metrics endpoint into the format a scraper expects.

## Startup and shutdown ordering

The ordering problem is the pattern's characteristic failure mode. If the application container starts before its proxy sidecar is ready, its first outbound calls fail; if the sidecar terminates before the application, the application's final requests and its last log lines are lost.

Kubernetes addresses this by modelling a sidecar as **an init container with `restartPolicy: Always`**. Ordinary init containers run to completion in sequence before the main containers start; an init container marked `Always` is instead started, and once it is ready the pod proceeds to the next init container and eventually to the main containers. It then keeps running for the life of the pod and is **terminated after the main containers have terminated**. Two further consequences follow from that classification: such a container supports probes, and its `startupProbe` is what the kubelet waits on before proceeding, so the ordering is gated by an explicit health check rather than approximated by a sleep, and its failure restarts the container rather than the whole pod.

```yaml
spec:
  initContainers:
    - name: tls-sidecar
      image: nginx:stable
      restartPolicy: Always         # makes this a sidecar, not a run-once init
      startupProbe:
        httpGet: { path: /healthz, port: 443, scheme: HTTPS }
  containers:
    - name: app
      image: legacy-app:1.4
```

The native mechanism is worth using in preference to hand-written readiness loops in the application's entrypoint, because those loops re-implement ordering inside the container whose independence from the sidecar is the point of the pattern.

## Pitfalls

- **Port collision inside the pod.** A second container that binds an already-bound port fails at startup with an address-in-use error, because the pod has one network namespace, not one per container.
- **The plaintext port remains reachable.** A TLS sidecar in front of an application listening on `0.0.0.0:8080` leaves the unencrypted listener open to anything that can reach the pod IP address; the sidecar adds a path rather than closing one.
- **A missing shared volume produces an empty log shipper.** A shipper mounted on its own image's `/var/log` reports no data and no error, because container filesystems are separate unless a pod-level volume is mounted into both.
- **Signal-based configuration reload silently does nothing** when `shareProcessNamespace` is unset: the sidecar cannot see, and therefore cannot signal, the application's process.
- **Resource limits are per container, not per pod.** A sidecar with no limit set can consume the node budget the application needs, and an under-requested proxy is throttled under exactly the load that makes the proxy matter.
- **Scaling is fused at 1:1.** A sidecar whose work is not proportional to the application's traffic — a periodic sync agent, say — is replicated once per pod, multiplying its cost by the replica count; such work belongs in a DaemonSet or a separate deployment.
- **A run-once init container is not a sidecar.** Omitting `restartPolicy: Always` on a long-running init container blocks the pod indefinitely, since the pod waits for an init container that never exits.
