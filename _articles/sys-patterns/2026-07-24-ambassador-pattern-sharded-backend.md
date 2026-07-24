---
title: "The ambassador pattern: give a dumb client a smart connection"
date: 2026-07-24
track: sys-patterns
summary: "An ambassador is an out-of-process proxy that owns the messy details of talking to a remote service — sharding, retries, TLS, request routing — so your app can open one plain connection to localhost and stay oblivious."
reading_time: 5
tags: [ambassador, envoy, sharding, proxy, patterns, burns]
sources:
  - title: "Brendan Burns, Designing Distributed Systems (2nd ed.) — Ambassadors (Ch. 4)"
    url: "https://www.oreilly.com/library/view/designing-distributed-systems/9781098156343/ch04.html"
  - title: "Envoy: Life of a Request"
    url: "https://www.envoyproxy.io/docs/envoy/latest/intro/life_of_a_request"
  - title: "Kubernetes: Sidecar Containers"
    url: "https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/"
---

Yesterday's sidecar rode alongside your app to add *inbound* capabilities. The **ambassador** is its twin for *outbound* traffic: a proxy container in the same pod that your app talks to over `localhost`, which then owns everything hard about reaching the real backend. Where a sidecar decorates what comes in, an ambassador decorates what goes out.

The move is the same — separation of concerns without a rewrite — but the payoff is different. Your app opens one boring TCP connection and forgets that the far side is sharded across ten nodes, sits behind mTLS, needs exponential-backoff retries, or is really "10% canary, 90% stable." All of that lives in the ambassador's lane.

## A concrete example: sharding a cache the app can't see

Say you have a Redis cluster split into shards, and a service that should route each key to the right shard. You do *not* want shard math baked into the app. Drop an ambassador beside it:

```yaml
apiVersion: v1
kind: Pod
metadata: { name: leaderboard }
spec:
  containers:
    - name: app                      # connects to redis at localhost:6379, knows nothing
      image: leaderboard:2.1
    - name: redis-ambassador         # the twist-cap proxy
      image: envoyproxy/envoy:v1.34-latest
      args: ["-c", "/etc/envoy/envoy.yaml"]
      ports: [{ containerPort: 6379 }]
      volumeMounts:
        - { name: cfg, mountPath: /etc/envoy, readOnly: true }
  volumes:
    - { name: cfg, configMap: { name: redis-ambassador-cfg } }
```

Envoy has a native Redis proxy filter that hashes each key to a shard for you. The app just does `redis.get("score:42")` against `localhost:6379`; the ambassador consistent-hashes `score:42` onto the correct upstream:

```yaml
# envoy.yaml (excerpt)
filters:
  - name: envoy.filters.network.redis_proxy
    typed_config:
      "@type": type.googleapis.com/envoy.extensions.filters.network.redis_proxy.v3.RedisProxy
      stat_prefix: redis
      prefix_routes: { catch_all_route: { cluster: redis_shards } }
# redis_shards cluster uses MAGLEV/ring-hash lb_policy across the shard endpoints
```

Change the shard count, swap the hash policy, or add TLS to the backend — the app image never rebuilds.

## Why it's worth recognizing

The ambassador is the outbound half of what a **service mesh** does: the same proxy that terminates inbound mTLS also load-balances, retries, and circuit-breaks your outbound calls. It's also how you test safely — point the ambassador at a mock upstream in dev, the real cluster in prod, with zero code change. And it makes polyglot fleets sane: write the connection smarts once, in one proxy, instead of re-implementing shard-aware clients in Go, Scala, and Python.

The cost is one extra hop of latency and a config file that is now load-bearing. For a single flat backend that's not worth it — talk to it directly. Reach for the ambassador when the *connection itself* has real logic that keeps leaking into your app.

**Try next:** run the pod above (or just Envoy locally in front of two `redis-server` instances on different ports) and watch keys land on different shards via `redis-cli MONITOR`. Once outbound routing lives in a proxy you can reason about, the third single-node pattern — the **adapter**, which normalizes what your app *emits* (metrics, logs) into a standard shape — completes the trio.
