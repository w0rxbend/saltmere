---
title: "Load Balancing: Layers, Algorithms, and Why Two Random Choices Wins"
date: 2026-08-10
track: microservices
summary: "The interview-staple tour of load balancing: L4 versus L7 and their trade-offs, the algorithm zoo from round-robin to least-connections to consistent hash, and a proper explanation of why power-of-two random choices beats both plain random and round-robin with only O(1) state."
reading_time: 6
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

"How would you load balance this?" is a question that rewards two moves: pick the right *layer*, then pick the right *algorithm*. Candidates who conflate the two — or who reach for round-robin reflexively — leave a lot on the table. The interesting part of load balancing is that the naive choices fail in specific, explainable ways, and the fixes are cheap.

## L4 versus L7: what the balancer can see

A **Layer 4 (transport) load balancer** operates on TCP/UDP. It makes one decision per *connection*, using only the 5-tuple (src/dst IP, src/dst port, protocol). It cannot see the HTTP request inside, so it cannot route by path or header — but it is cheap, fast, and protocol-agnostic (it balances anything: gRPC streams, databases, raw TCP).

Two common L4 datapaths:

- **NAT mode**: the balancer rewrites the destination IP and sits in the return path, so replies flow back through it. Simple, but the balancer is a bottleneck for response bytes.
- **DSR (Direct Server Return)**: the balancer rewrites only the L2 destination and the backend replies *directly* to the client, bypassing the balancer entirely on the way out. Since responses are usually far larger than requests, DSR lets a modest balancer front enormous egress. The cost is operational: backends need the VIP configured on a loopback and ARP suppressed, and you lose per-response visibility.

A **Layer 7 (application) load balancer** terminates the connection and parses HTTP. Now it can route by URL path, `Host`, header, cookie, or method; do TLS termination, retries, per-request timeouts, and header-based canary routing; and — crucially — balance at the granularity of *requests*, not connections. That matters enormously for HTTP/2 and gRPC, where thousands of requests multiplex over one long-lived connection: an L4 balancer would pin all of them to whichever backend won the connection lottery. The cost is CPU (parsing, TLS) and that it only understands the protocols it implements.

The interview one-liner: **L4 is per-connection and blind but fast; L7 is per-request and smart but expensive.** Real stacks often chain them — an L4 balancer spreads connections across a fleet of L7 proxies.

## The algorithm zoo

Once you have picked a layer, you pick how to choose a backend. The honest framing is *how much state does the algorithm need, and how does it behave under variance* (uneven request costs, slow backends, a freshly deployed instance with a cold cache).

| Algorithm | State needed | When it wins | Failure mode |
|---|---|---|---|
| Round-robin | none (a counter) | uniform request cost, homogeneous backends | ignores actual load; a slow request still gets its "turn" |
| Weighted round-robin | static weights | heterogeneous instance sizes | weights are static; can't react to live load |
| Least-connections | live conn/req count | variable request durations | thundering herd onto a fresh/empty node (see below) |
| Least-response-time (EWMA) | smoothed latency | latency matters, backends differ | needs measurement; stale estimates cause herding |
| Random | none | large fleets, want statelessness | high variance; unlucky node gets a burst |
| Power of two choices (P2C) | O(1) per pick | near-optimal balance, cheap | still needs a load signal per host |
| Consistent / IP hash | ring / key | session or cache affinity | uneven keys → hot shards |

A few notes. **Least-connections** is the default smart choice and, in HAProxy's own testing, beats P2C by roughly 4% on response time when the live counts are accurate. **Least-response-time** (NGINX's `least_time`, using an EWMA over header or last-byte latency) is what you want when backends have genuinely different speeds — a smoothed latency estimate reacts to a degrading node that connection counts alone miss. **Hashing** is the odd one out: it exists not to balance load but to create *affinity*.

## Why power of two random choices wins

Here is the result worth memorizing. Plain random has bad tail behavior: with *n* balls into *n* bins, the fullest bin holds about `log n / log log n` balls. Mitzenmacher's **power of two choices** result: if instead you pick **two** bins at random and place the ball in the *less loaded* one, the max load drops to `log log n / log 2 + O(1)` — an *exponential* improvement, from logarithmic to double-logarithmic. Going from two choices to three barely helps; the big jump is from one to two.

The intuition (Marc Brooker put it well) is that P2C threads a needle. Plain **random** wastes capacity because it ignores load — an idle node and an overloaded node are equally likely to be picked. Greedy **"query everyone, pick the emptiest"** looks ideal but, with any stale load data, *herds*: every balancer sees the same node as least-loaded and stampedes it, then the crowd swings to the next quiet node — oscillation. **P2C uses real load information (unlike random) but rejects herding (unlike greedy):** because each request samples a different random pair, no single "winner" attracts the whole fleet at once.

And it needs only **O(1) state per decision** — no global scan, no coordination. That is why it is the workhorse in production proxies: Envoy's "least request" load balancer with equal weights *is* P2C by default (it calls the full scan "O(N)" and P2C "nearly as good" with "resistance to herding behavior"), and NGINX's `random two` picks two servers at random and breaks the tie with `least_conn`.

```text
# Power of two choices (P2C)
# hosts: list of healthy backends; load(h): active requests / conns / EWMA latency
function pick(hosts):
    if hosts is empty: reject
    a = random_choice(hosts)
    b = random_choice(hosts)      # sample with replacement is fine at scale
    while b == a and len(hosts) > 1:
        b = random_choice(hosts)
    return a if load(a) <= load(b) else b
```

That is the whole algorithm. No sort, no global counter, no locks.

## Health checks and the deploy gotcha

None of this matters if you route to a dead node. Balancers run **active** health checks (periodic probes to a `/healthz`-style endpoint) and **passive** checks (eject a host after N consecutive request failures, a circuit-breaker style outlier detection). The subtlety: a health check should test *readiness to serve*, not mere liveness — a node with a cold cache or a warming JIT is "up" but slow, and dumping traffic on it is a self-inflicted latency spike.

Which is the classic **least-connections thundering herd**. When you deploy or autoscale a new instance, its active-connection count is *zero* — the global minimum. A least-connections (or naive least-request) balancer therefore routes *every* new request to it, because it looks emptiest, until it drowns. The fixes: **slow-start**, ramping a new host's effective weight from 0 to full over a window (HAProxy and NGINX both do this), and preferring P2C, which by construction only ever sends a fresh node the requests where it happens to win one of two random draws — a trickle, not a flood.

## Consistent hashing: balancing for affinity

Sometimes you *want* the same key to land on the same backend — to hit a warm cache, keep a session sticky, or shard state. Plain modulo hashing remaps almost everything when the fleet changes size. **Consistent hashing** (and **rendezvous/HRW**) remap only the minimum, which is why they front cache tiers and sharded stores. Envoy's ring-hash and Maglev balancers, and NGINX's `hash $key consistent`, exist for exactly this. The tension: affinity fights balance — a hot key makes a hot shard, and no clever hash saves you from a skewed keyspace. For the mechanics, see [consistent hashing](/articles/distributed-systems/2026-07-25-consistent-hashing-ring) and [rendezvous (HRW) hashing](/articles/distributed-systems/2026-08-10-rendezvous-hrw-hashing).

**Try next:** wire a P2C balancer in front of three backends, give one an artificial 200 ms delay, and watch how it starves that node of traffic that round-robin would happily keep feeding it.
