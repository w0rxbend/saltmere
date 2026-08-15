---
title: "Load Balancing: Layers, Algorithms, and the Power of Two Random Choices"
date: 2026-08-10
track: microservices
summary: "A tour of load balancing: layer 4 versus layer 7 and their trade-offs, the algorithm space from round-robin to least-connections to consistent hash, and why power-of-two random choices improves on both plain random and greedy selection with O(1) state per decision."
reading_time: 7
tags: [load-balancing, l4-l7, power-of-two-choices, consistent-hashing, microservices]
sources:
  - title: "Mitzenmacher, The Power of Two Choices in Randomized Load Balancing (IEEE TPDS 2001)"
    url: "https://www.eecs.harvard.edu/~michaelm/postscripts/tpds2001.pdf"
  - title: "Marc Brooker, The power of two random choices"
    url: "https://brooker.co.za/blog/2012/01/17/two-random.html"
  - title: "HAProxy, Test driving power of two random choices load balancing"
    url: "https://www.haproxy.com/blog/power-of-two-load-balancing"
  - title: "Envoy, Supported load balancers (Weighted Least Request / P2C)"
    url: "https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/load_balancing/load_balancers"
  - title: "NGINX, ngx_http_upstream_module (methods: least_conn, least_time, hash, random two)"
    url: "https://nginx.org/en/docs/http/ngx_http_upstream_module.html"
---

**Gist.** A load balancer must place each unit of work on a backend without global knowledge of every backend's current load. Two decisions determine behaviour: the *layer* at which the balancer operates, which fixes the granularity of the unit (connection or request), and the *selection rule*, which trades state and coordination against the resulting maximum load. Every rule pays for its balance quality somewhere — in per-host measurement, in stale-data herding, or in a longer tail on the fullest backend.

## Layer 4 versus layer 7: what the balancer can observe

A **layer 4 (transport) load balancer** operates on TCP and UDP. It makes one decision per *connection*, using only the 5-tuple: source and destination address, source and destination port, and protocol. It cannot observe the HTTP request carried inside, so it cannot route by path or header. It is correspondingly cheap and protocol-agnostic: it balances gRPC streams, database connections and raw TCP alike.

Two common layer 4 datapaths:

- **Network address translation (NAT) mode**: the balancer rewrites the destination address and remains in the return path, so replies traverse it. **Response bytes therefore consume balancer capacity.**
- **Direct server return (DSR)**: the balancer rewrites only the layer 2 destination, and the backend replies directly to the client, bypassing the balancer on egress. Where responses are larger than requests, this removes the response volume from the balancer's budget. The costs are operational: **each backend needs the virtual IP (VIP) configured on a loopback interface with Address Resolution Protocol (ARP) suppression**, and the balancer observes no responses, so passive per-response signals are unavailable.

A **layer 7 (application) load balancer** terminates the connection and parses HTTP. It can route on URL path, `Host`, header, cookie or method; terminate TLS; apply retries and per-request timeouts; and select a backend per *request* rather than per connection. **That granularity is load-bearing for HTTP/2 and gRPC, where many requests multiplex over one long-lived connection**: a layer 4 balancer binds all of them to the single backend that won the connection's placement, and the imbalance persists for the connection's lifetime. The costs are CPU for parsing and TLS, and coverage limited to the protocols the proxy implements.

The compact statement: **layer 4 is per-connection and blind; layer 7 is per-request and protocol-aware at higher CPU cost.** The two compose — a layer 4 tier spreading connections across a fleet of layer 7 proxies.

## The selection rules

The useful axis is *how much state the rule requires* and *how it behaves under variance* — unequal request costs, a degrading backend, or a freshly started instance with a cold cache.

| Algorithm | State needed | When it wins | Failure mode |
|---|---|---|---|
| Round-robin | none (a counter) | uniform request cost, homogeneous backends | ignores actual load; a slow backend still receives its turn |
| Weighted round-robin | static weights | heterogeneous instance sizes | weights are static and do not react to live load |
| Least-connections | live connection or request count | variable request durations | herding onto a freshly started, empty node |
| Least-response-time (EWMA) | smoothed latency per host | backends with genuinely different speeds | stale estimates cause herding |
| Random | none | large fleets, no per-host state | high variance; an unlucky node receives a burst |
| Power of two choices (P2C) | O(1) per decision | balance close to greedy, without a scan | still requires a load signal per host |
| Consistent or IP hash | ring or key mapping | session or cache affinity | skewed keys produce hot shards |

**Least-connections** is the common default among load-aware rules; in HAProxy's own test drive of power-of-two random choices, least-connections remained the stronger performer where a single balancer held accurate live counts, with P2C close behind and well ahead of plain random. **Least-response-time** — NGINX's `least_time`, using an exponentially weighted moving average (EWMA) over header or last-byte latency — carries information that connection counts do not: a node that is slow but not saturated. **Hashing is not a balancing rule at all**; it exists to produce affinity, and its load distribution is whatever the key distribution happens to be.

## Power of two random choices

The result is about maximum load, not mean load. Placing *n* balls into *n* bins uniformly at random leaves the fullest bin holding about `log n / log log n` balls. The two-choice result, surveyed by Mitzenmacher: sampling **two** bins at random and placing the ball in the less loaded one drops the maximum to `log log n / log 2 + O(1)` — a reduction from logarithmic to double-logarithmic. Sampling *d* bins instead of two replaces `log 2` with `log d` in the denominator, so **the jump is from one sample to two; further samples move only a constant factor.**

The qualitative comparison, as Brooker states it, is that P2C occupies the space between two failing extremes. Plain **random** discards load information entirely: an idle backend and a saturated one are equally likely. Greedy **scan-and-pick-the-emptiest** uses all the information but, when load data is stale — which it is whenever several balancers decide independently — every balancer identifies the same minimum and directs traffic there simultaneously, then to whichever node becomes quietest next. **P2C uses the load signal but decorrelates the decisions**: each request samples a different random pair, so no single node is the minimum of every balancer's comparison at once.

The cost side is small: **one comparison and two random draws, with no global scan and no coordination between balancers.** Envoy's least-request load balancer with equal weights performs P2C by default, describing the full scan as O(N) and P2C as nearly as good with resistance to herding behaviour. NGINX's `random two` selects two servers at random and resolves the pair with `least_conn`.

### Implementation sketch (Scala)

```scala
final case class Host(id: String, inFlight: java.util.concurrent.atomic.AtomicLong)

final class P2C(hosts: IndexedSeq[Host], rng: scala.util.Random):

  /** Load signal read per decision; any monotone "busier is larger" metric works. */
  private def load(h: Host): Long = h.inFlight.get()

  def pick(): Option[Host] = hosts.size match
    case 0 => None
    case 1 => Some(hosts(0))
    case n =>
      val i = rng.nextInt(n)
      // Draw the second index from n-1 slots and skip i: uniform over the
      // distinct hosts, and terminates, unlike rejection sampling in a loop.
      val j = { val k = rng.nextInt(n - 1); if k >= i then k + 1 else k }
      val a = hosts(i)
      val b = hosts(j)
      Some(if load(a) <= load(b) then a else b)

  def dispatch[A](call: Host => A): Option[A] =
    pick().map: h =>
      h.inFlight.incrementAndGet()
      try call(h) finally h.inFlight.decrementAndGet()
```

The counter must be incremented before the call and decremented in a `finally`; **a leaked increment makes a healthy host look permanently busy and removes it from selection.**

## Health checking and the empty-node case

Selection is only correct over hosts that can serve. Balancers run **active** health checks — periodic probes of an endpoint — and **passive** checks, ejecting a host after consecutive request failures, in the style of outlier detection. The distinction that matters is between liveness and readiness: **a node with a cold cache or an unwarmed JIT compiler (a runtime compiler that optimizes code only after observing it execute) answers a probe while still serving slowly**, and routing full traffic to it produces a latency spike.

That is the mechanism behind the least-connections herd on deployment. A newly started instance has an active-connection count of zero, which is the global minimum. **A least-connections or least-request rule directs every arriving request to it until its count rises above the others' — and the count only rises as requests are accepted, so the correction lags the flood.** Two mitigations exist: **slow-start**, which ramps a new host's effective weight from zero to full over a window (HAProxy's `slowstart`; NGINX documents `slow_start` as part of its commercial subscription), and P2C, which by construction sends the new node only the requests where it is drawn as one of two candidates.

## Consistent hashing: placement for affinity

Where the same key should reach the same backend — to hit a warm cache, to keep a session on one node, or to shard state — the placement rule must be stable under fleet changes. Plain modulo hashing remaps nearly every key when the host count changes. **Consistent hashing** and **rendezvous (highest random weight, HRW) hashing** remap close to the minimum number of keys, which is why they front cache tiers and sharded stores. Envoy's ring-hash and Maglev balancers and NGINX's `hash $key consistent` implement this class. The tension is structural: **affinity and balance are opposed — a hot key produces a hot shard, and no choice of hash function corrects a skewed keyspace.** For the mechanics, see [consistent hashing](/articles/distributed-systems/2026-07-25-consistent-hashing-ring) and [rendezvous (HRW) hashing](/articles/distributed-systems/2026-08-10-rendezvous-hrw-hashing).

## Pitfalls

- **Layer 4 balancing in front of gRPC or HTTP/2 leaves backends unevenly loaded even under uniform traffic**, because placement happens once per long-lived connection and every multiplexed request inherits it.
- **Least-connections concentrates traffic on a newly deployed instance**, because zero in-flight requests is the global minimum and the counter rises only after requests have already been accepted.
- **A liveness probe that returns success before caches or the JIT compiler are warm admits a slow node into rotation**; the symptom is a latency spike correlated with deploys rather than with traffic.
- **A leaked in-flight counter increment (an exception path that skips the decrement) permanently inflates a host's apparent load** and silently removes it from least-connections and P2C selection.
- **Greedy least-loaded selection across multiple independent balancers oscillates** rather than converging, because all of them read the same stale minimum and act on it simultaneously.
- **Direct server return removes response traffic from the balancer's view**, so passive health signals derived from responses no longer exist and misconfigured loopback VIP or ARP settings on a backend appear as unexplained connection blackholing.
- **Consistent hashing does not balance load**; a skewed key distribution produces a hot shard whose only remedies are key splitting or replication, not a different hash function.
