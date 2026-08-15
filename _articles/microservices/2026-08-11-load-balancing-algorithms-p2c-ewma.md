---
title: "Load Balancing: From Round-Robin to the Power of Two Choices"
date: 2026-08-11
track: microservices
summary: "Global least-loaded balancing produces herding, and maintaining a consistent global view is expensive. The power of two choices samples two backends at random and routes to the less loaded one, reducing maximum load from log n / log log n to log log n / log 2 + O(1) at O(1) cost per request."
reading_time: 7
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

**Gist.** A layer-7 balancer must choose a backend for every request, and the two obvious extremes both fail: uniform random ignores load, while routing to the globally least-loaded backend requires a fresh consistent view of the fleet and causes independent balancers to stampede the same instance. The power of two choices (P2C) samples two backends uniformly at random and routes to the less loaded of the pair, which lowers the maximum load from **log n / log log n** to **log log n / log 2 + O(1)** with high probability. The cost is that the decision is provably near-optimal rather than optimal, and the quality of the result depends entirely on the load metric being compared.

## Layer 4 versus layer 7

**Layer-4 (L4)** balancers — AWS Network Load Balancer, IPVS, most hardware appliances — route on IP address and port. A backend is chosen once per TCP connection and every byte of that connection is pinned to it. The balancer is protocol-agnostic and cheap, and it cannot observe individual requests.

**Layer-7 (L7)** balancers — Envoy, Nginx, HAProxy, gRPC client-side balancing — parse HTTP or gRPC and choose per request. **Load-aware algorithms require L7**, because active-request counts and per-request latency are only observable when requests are distinguishable from connections. Everything below assumes L7.

## Baseline algorithms

- **Round-robin (RR).** Backends are handed out in rotation. State is zero; a degraded backend receives the same share as a healthy one.
- **Weighted round-robin.** Static weights give larger instances more slots. This handles heterogeneous hardware and still ignores live load.
- **Least-connections / least-request.** The next request goes to the backend with the fewest in-flight requests. This is the first algorithm whose decision depends on observed load.

## Why the global minimum is the wrong target

Extending least-request to "always route to the fleet-wide minimum" degrades in two distinct ways.

**Herding.** When many balancers — separate proxies, or separate threads within one proxy — read the same load view, they all identify the same least-loaded backend and dispatch to it simultaneously. The counters for that backend only rise after the requests land, so by the time the view refreshes, the chosen instance is the most loaded one and the herd migrates to the next. **Load oscillates instead of converging**, and the oscillation period is set by the counter propagation delay, not by the traffic.

**Cost of the global view.** Identifying the exact minimum requires every balancer to hold a fresh, consistent snapshot of every backend's load. That is continuous cross-fleet gossip, and **the snapshot is stale at the instant it is read** — the decision is made against a state that no longer holds.

## The power of two choices

The construction, due to Mitzenmacher, abandons the global minimum. **Two backends are drawn uniformly at random and the request is routed to the less loaded of the two.**

The two draws must be distinct — sampling with replacement collapses the pair into a single choice — and the comparison key is whatever load metric the balancer maintains: active request count, or the peak-EWMA cost described below. No scan and no coordination occur; the work per request is two samples and one comparison. The sampling is what suppresses herding: **two independent balancers rarely draw the same pair**, so their decisions decorrelate without any communication between them.

The analytical result is the balls-and-bins bound. Throwing *n* balls into *n* bins with each bin chosen uniformly at random leaves the fullest bin holding roughly **log n / log log n** balls with high probability. If each ball instead samples *two* bins and enters the emptier one, the maximum load falls to **log log n / log 2 + O(1)** — an exponential reduction obtained from one additional sample. Increasing the sample count to three or four improves the bound only by a constant factor; **the discontinuity is between one choice and two**.

## Deployments

**Envoy's** `LEAST_REQUEST` policy is P2C when host weights are equal: it "selects N random available hosts (2 by default) and picks the host which has the fewest active requests." With unequal weights the policy switches to a dynamically-weighted round-robin. Envoy's `RANDOM` policy is single-choice uniform random. **Nginx** ships `least_conn`, and its `random two least_conn` directive is P2C over in-flight connections. **gRPC** performs balancing in the client, so the pick runs once per client rather than in a shared proxy — a shape in which herding depends on how many clients share a load view.

## Peak-EWMA: latency as the load metric

In-flight count does not distinguish a fast backend holding *k* requests from a slow one holding *k*. **Peak-EWMA**, from Twitter's Finagle and adopted by Linkerd, replaces the count with an **exponentially-weighted moving average (EWMA) of the backend's round-trip latency, weighted by outstanding requests**, and uses that cost as the comparison key inside P2C. Recent samples dominate the average, so a backend that slows — a garbage-collection pause, a degraded node or disk — has its cost rise and **begins shedding traffic before any health check marks it unhealthy**. The state is per-backend and local to the balancer, so the O(1) property of P2C is preserved.

Linkerd's published benchmark introduces one artificially slowed replica and reports peak-EWMA holding latency across higher percentiles than round-robin or least-loaded; the exact percentile figures are in the post rather than reproduced here.

| Algorithm | State needed | Reacts to load | Reacts to latency | Herd risk |
|---|---|---|---|---|
| Round-robin | none | no | no | none |
| Weighted RR | static weights | no | no | none |
| Least-request (global) | all backends | yes | no | high |
| **P2C least-request** | 2 samples | yes | no | low |
| **P2C peak-EWMA** | 2 samples + EWMA | yes | **yes** | low |

### Implementation sketch (Scala)

```scala
final class Backend(val id: String):
  private val inflight = java.util.concurrent.atomic.AtomicInteger(0)
  @volatile private var ewmaNanos: Double = 0.0
  @volatile private var lastNanos: Long   = System.nanoTime()

  // Cost compared inside P2C: latency estimate scaled by queue depth.
  def cost: Double = ewmaNanos * (inflight.get() + 1)

  def acquire(): Unit = inflight.incrementAndGet()

  def release(rttNanos: Long, tauNanos: Double): Unit =
    inflight.decrementAndGet()
    val now = System.nanoTime()
    val dt  = (now - lastNanos).toDouble
    lastNanos = now
    // Decay depends on elapsed time, so idle backends forget an old peak.
    val w = math.exp(-dt / tauNanos)
    ewmaNanos = ewmaNanos * w + rttNanos * (1 - w)

def pick(backends: IndexedSeq[Backend], rnd: scala.util.Random): Backend =
  val n = backends.length
  if n == 1 then backends(0)
  else
    val i = rnd.nextInt(n)
    var j = rnd.nextInt(n - 1)
    if j >= i then j += 1              // draw without replacement, no retry loop
    val (a, b) = (backends(i), backends(j))
    if a.cost <= b.cost then a else b
```

## When affinity beats balancing

Even spreading is not always the objective: routing a key to the same backend keeps that backend's cache warm. That is **consistent hashing**, which trades balance for cache hit rate; the ring construction is in [the consistent-hashing article](/saltmere/articles/distributed-systems/2026-07-25-consistent-hashing-ring/). Bounded-load consistent hashing combines the two by falling back to a second candidate when the primary exceeds its load bound.

## Pitfalls

- **Sampling with replacement.** Drawing the same backend twice makes the request a single-choice random pick, silently reverting the log log *n* bound to log n / log log n for that fraction of traffic.
- **Comparing counters that are not updated in the same place.** If the in-flight counter is incremented after dispatch rather than at pick time, concurrent picks read a stale zero and both route to the same backend — herding reappears inside a single balancer.
- **Least-request with unequal weights on Envoy.** `LEAST_REQUEST` stops being P2C when host weights differ; it becomes a dynamically-weighted round-robin, so a configuration change that introduces weights changes the algorithm.
- **A backend that fails fast wins every comparison.** Errors returning in microseconds drive both the in-flight count and the latency EWMA down, so the fastest-failing instance attracts the most traffic unless failures are excluded from the metric or handled by outlier ejection.
- **Idle backends holding a stale EWMA.** An instance that receives no traffic keeps whatever cost it last recorded; without time-based decay a formerly slow backend is never reconsidered, and a formerly fast one absorbs a burst it can no longer serve.
- **Connection-level balancing under HTTP/2 or gRPC.** Long-lived multiplexed connections pin many requests to one backend chosen once, so an L4 balancer in front of an L7 protocol makes the per-request algorithm irrelevant.
