---
title: "Load Balancing: From Round-Robin to the Power of Two Choices"
date: 2026-08-11
track: microservices
summary: "Pure least-loaded balancing causes herds; global state is expensive. The power of two choices — pick two backends at random, send to the less loaded — gets you near-optimal spread for O(1) work. Here's the algorithm, the math, and how Envoy and Linkerd ship it."
reading_time: 6
tags:
  - load-balancing
  - scaling
  - envoy
  - service-mesh
  - grpc
sources:
  - title: "Mitzenmacher, Richa, Sitaraman — The Power of Two Random Choices: A Survey (Harvard)"
    url: "https://www.eecs.harvard.edu/~michaelm/postscripts/handbook2001.pdf"
  - title: "Mitzenmacher — The Power of Two Choices in Randomized Load Balancing (IEEE TPDS 2001)"
    url: "https://cs.colby.edu/courses/F09/cs231-labs/labs/lab07/Mitzenmacher-2Choices-TPDS2001.pdf"
  - title: "Envoy — Supported load balancers (weighted least request / P2C)"
    url: "https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/load_balancing/load_balancers.html"
  - title: "Linkerd — Beyond Round Robin: Load Balancing for Latency (peak-EWMA)"
    url: "https://linkerd.io/2016/03/16/beyond-round-robin-load-balancing-for-latency/"
  - title: "Finagle — Balancers (P2C, peak-EWMA, aperture) API docs"
    url: "https://twitter.github.io/finagle/docs/com/twitter/finagle/loadbalancer/Balancers$.html"
---

A load balancer answers one question thousands of times a second: *which backend gets this request?* Get it wrong and you overload a hot instance while others idle, or you pile requests onto a backend that quietly went slow. This is a system-design interview staple, and the interesting answer isn't round-robin — it's **the power of two choices**.

## L4 vs L7, briefly

**L4** balancers (AWS NLB, IPVS, most hardware LBs) route by IP and port. They pick a backend once per TCP connection and pin every byte of that connection to it. Cheap, fast, protocol-agnostic — but blind to what's inside.

**L7** balancers (Envoy, Nginx, HAProxy, gRPC's client-side LB) parse HTTP/gRPC and route *per request*. That's what makes the smart algorithms below possible: you can only balance by active request count or latency if you can see individual requests, not just connections. Everything that follows assumes L7.

## The simple algorithms

- **Round-robin (RR):** hand out backends in rotation. Zero state, but treats a struggling backend the same as a healthy one.
- **Weighted RR:** give bigger instances more slots. Handles heterogeneous hardware; still ignores live load.
- **Least-connections / least-request:** send the next request to the backend with the fewest in-flight requests. This is the first algorithm that reacts to *actual* load, and it's genuinely good — until you make it global.

## Why "always pick the least loaded" is a trap

The obvious upgrade is: track every backend's load and always route to the global minimum. In a distributed fleet this backfires two ways.

First, **the herd problem.** If many balancers (or many threads) all see the same "least loaded" backend, they all stampede it simultaneously. By the time load counters update, that backend is now the *most* loaded, so the herd swings to the next victim. Load oscillates instead of settling.

Second, **global state is expensive.** Knowing the exact least-loaded backend means every balancer needs a fresh, consistent view of every backend — constant chatter, and it's stale the moment you read it.

## The power of two choices

Here's the trick, due to Mitzenmacher: don't find the global minimum. **Pick two backends uniformly at random, and route to the less loaded of the two.** Also called P2C or "two random choices."

```
# Power of Two Choices (P2C) — O(1) per request
def choose(backends):
    a = random.choice(backends)
    b = random.choice(backends)          # sample without replacement
    while b is a and len(backends) > 1:
        b = random.choice(backends)
    # "load" = active/in-flight requests (or peak-EWMA cost, below)
    return a if a.load <= b.load else b
```

That's it. No global scan, no coordination — just two samples and a comparison. The randomness breaks up herds: two balancers rarely draw the same pair, so they don't stampede the same backend.

The math is the surprising part. Throw *n* balls into *n* bins choosing each bin uniformly at random, and the fullest bin holds about **log n / log log n** balls with high probability. Now let each ball pick *two* bins at random and drop into the emptier one: the max load collapses to **log log n / log 2 + O(1)**. For a million backends that's roughly the difference between a worst-case pile of ~20 and one of ~5 — an exponential improvement, from *one extra sample*. Adding a third or fourth choice only shaves off a constant factor after that; two is where the leverage is.

So P2C buys you near-least-loaded quality at random's cost. That's why it's the default under the hood of production balancers, not a curiosity.

## Where it lives in practice

**Envoy's** `LEAST_REQUEST` policy is P2C when weights are equal: it "selects N random available hosts (2 by default) and picks the host which has the fewest active requests." With unequal weights it switches to a dynamically-weighted RR. Its `RANDOM` policy is plain single-choice random. **Nginx** ships `least_conn`; the community and commercial builds expose two-choices variants. **gRPC** does client-side L7 balancing, so each client runs the pick.

## Peak-EWMA: balancing by latency, not just count

In-flight count is a decent proxy for load, but it can't tell a fast backend from a slow one holding the same number of requests. **Peak-EWMA** (from Twitter's Finagle, adopted by Linkerd) fixes that: maintain an **exponentially-weighted moving average of each backend's round-trip latency**, weighted by outstanding requests, and use *that cost* as the "load" in P2C. Recent samples dominate the average, so a backend that suddenly slows — GC pause, a bad node, a degraded disk — sees its cost spike and immediately starts shedding traffic to healthier peers, without being marked fully unhealthy.

Linkerd's own benchmark makes the point: with one replica artificially slowed to 2s, round-robin degraded at the 95th percentile and least-loaded at the 99th, while peak-EWMA held speed to the **99.9th** percentile. Envoy now ships a contrib peak-EWMA policy too.

| Algorithm | State needed | Reacts to load | Reacts to latency | Herd risk |
|---|---|---|---|---|
| Round-robin | none | no | no | none |
| Weighted RR | static weights | no | no | none |
| Least-request (global) | all backends | yes | no | high |
| **P2C least-request** | 2 samples | yes | no | low |
| **P2C peak-EWMA** | 2 samples + EWMA | yes | **yes** | low |

## When affinity beats balancing

Sometimes you *don't* want to spread evenly — you want the same key to hit the same backend so its cache stays warm. That's **consistent hashing** (and Google's Maglev for even, minimal-disruption L4 spread). It trades some balance for cache hit rate; see the ring construction in [the consistent-hashing article](/saltmere/articles/distributed-systems/2026-07-25-consistent-hashing-ring/). Real systems often combine the two — bounded-load consistent hashing falls back to a second choice when the primary is overloaded, which is P2C wearing a hash for a hat.

The interview takeaway: name round-robin, explain why global least-loaded herds, then reach for P2C and justify it with the log-log-n result. Add peak-EWMA when latency (not just count) is the signal that matters.

**Try next:** wire a P2C picker into a small gRPC client, add a latency EWMA per backend, then inject a 500ms delay into one instance and watch traffic drain off it.
