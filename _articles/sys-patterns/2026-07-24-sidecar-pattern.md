---
title: "The sidecar pattern: add capabilities to a service without touching its code"
date: 2026-07-24
track: sys-patterns
summary: "A sidecar is a second container that rides alongside your app in the same pod, sharing its network and disk. It's how you bolt on TLS, log shipping, or a proxy to a service you can't or won't modify."
reading_time: 4
tags: [sidecar, kubernetes, containers, patterns, burns]
sources:
  - title: "Brendan Burns, Designing Distributed Systems (2nd ed.) — Single-Node Patterns"
    url: "https://www.oreilly.com/library/view/designing-distributed-systems/9781098156343/"
  - title: "Kubernetes: Sidecar Containers"
    url: "https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/"
---

The first single-node pattern in Burns' book is also the one you'll reach for most: the **sidecar**. The idea is simple — deploy a second container in the same unit (a Kubernetes pod) as your main application. Because they share the pod's network namespace and can share a volume, the sidecar can see `localhost` traffic and files exactly as the app does, yet it ships and scales as its own image.

The payoff is *separation of concerns without a rewrite*. Your app keeps doing its one job; cross-cutting capabilities — TLS termination, log collection, config reload, a caching proxy — live in the sidecar, owned and versioned separately.

## A concrete example: HTTPS for a plain-HTTP app

You have a legacy service that speaks only HTTP on port 8080 and you can't touch it. Add an nginx sidecar that terminates TLS and proxies to `localhost:8080`:

```yaml
apiVersion: v1
kind: Pod
metadata: { name: legacy-app }
spec:
  containers:
    - name: app                     # unchanged, HTTP-only
      image: legacy-app:1.4
      ports: [{ containerPort: 8080 }]
    - name: tls-sidecar             # rides alongside, same network
      image: nginx:stable
      ports: [{ containerPort: 443 }]
      volumeMounts:
        - { name: certs, mountPath: /etc/nginx/certs, readOnly: true }
        - { name: conf,  mountPath: /etc/nginx/conf.d, readOnly: true }
  volumes:
    - { name: certs, secret: { secretName: app-tls } }
    - { name: conf,  configMap: { name: nginx-proxy-conf } }
```

The nginx config just does `proxy_pass http://localhost:8080;` behind a `listen 443 ssl;` block. The app never learns TLS exists. Swap the cert, upgrade nginx, or add rate-limiting entirely in the sidecar's lane.

## Why this is more than a trick

The sidecar is the mechanism behind things you already use. A **service mesh** (Istio, Linkerd) is, at bottom, a proxy sidecar injected into every pod that transparently handles mTLS, retries, and telemetry — the app is oblivious. Log shippers, secret-rotation agents, and config-sync daemons are all sidecars. Recognizing the pattern turns "how does the mesh do that?" from magic into a container you could have written.

One caveat worth knowing: startup and shutdown ordering used to be fiddly (the app could start before its proxy was ready). Kubernetes now models sidecars as a special kind of init container so they start first and stop last — use that native support rather than hand-rolling readiness hacks.

**Try next:** wrap any HTTP-only container you have with the nginx sidecar above and hit it over HTTPS. Once you've seen your app get a capability it has no code for, the ambassador and adapter patterns (outbound proxying and interface normalization) will read as obvious variations on the same move.
