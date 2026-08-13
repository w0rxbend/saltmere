---
title: "The 8 fallacies of distributed computing, thirty years on: every one still has your pager number"
date: 2026-08-13
track: sys-patterns
summary: "Peter Deutsch's seven fallacies (plus Gosling's eighth) are a checklist of assumptions that compile fine and fail in production. Each one maps to a modern incident pattern — and to a mitigation this corpus already covers. Then we make one bite on purpose with tc netem."
reading_time: 5
tags: [fallacies, distributed-systems, resiliency, chaos-engineering, netem]
sources:
  - title: "Rotem-Gal-Oz — Fallacies of Distributed Computing Explained"
    url: "https://arnon.me/wp-content/uploads/Files/fallacies.pdf"
  - title: "Wikipedia — Fallacies of distributed computing"
    url: "https://en.wikipedia.org/wiki/Fallacies_of_distributed_computing"
  - title: "Bailis & Kingsbury — The Network is Reliable (ACM Queue)"
    url: "https://queue.acm.org/detail.cfm?id=2655736"
  - title: "Marc Brooker (AWS) — Timeouts, retries, and backoff with jitter"
    url: "https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/"
  - title: "man 8 tc-netem — Network Emulator"
    url: "https://man7.org/linux/man-pages/man8/tc-netem.8.html"
---

In the early 1990s at Sun, L. Peter Deutsch wrote down seven assumptions that "essentially everyone" makes when they first build a distributed application, building on an earlier list by Bill Joy and Tom Lyon; James Gosling added the eighth in 1997. Arnon Rotem-Gal-Oz's 2006 paper [*Fallacies of Distributed Computing Explained*](https://arnon.me/wp-content/uploads/Files/fallacies.pdf) is still the best walkthrough of why each one hurts. The list survives because it isn't trivia — it's the taxonomy behind most of the failure modes Brendan Burns' patterns exist to contain. The wrapper changed (Kubernetes, meshes, serverless); the physics didn't.

## The eight, mapped to what actually pages you

| # | Fallacy | Modern bite | Counter-pattern in this corpus |
|---|---------|-------------|-------------------------------|
| 1 | The network is reliable | Cross-AZ partition wedges leader election; requests vanish mid-flight | [Timeouts + jittered retries](/articles/microservices/2026-07-26-timeouts-retries-bulkheads), [circuit breakers](/articles/microservices/2026-07-24-circuit-breakers-resilience4j) |
| 2 | Latency is zero | A "fast" endpoint makes 30 sequential RPCs; cross-region hop adds ~80 ms each | [Scatter/gather](/articles/sys-patterns/2026-07-26-scatter-gather-pattern), [request hedging](/articles/sys-patterns/2026-07-30-request-hedging-tail-latency) |
| 3 | Bandwidth is infinite | Fat payloads on every event saturate a NIC; a retry storm amplifies it | [Claim check](/articles/sys-patterns/2026-07-31-claim-check-pattern), [backpressure](/articles/sys-patterns/2026-07-31-backpressure-flow-control) |
| 4 | The network is secure | Flat internal network; one SSRF pivots to everything | [Zero-trust mTLS + JWT](/articles/microservices/2026-07-26-zero-trust-mtls-jwt) |
| 5 | Topology doesn't change | Cached DNS/IPs point at rescheduled pods; conns pinned to drained nodes | [Replicated, health-checked serving](/articles/sys-patterns/2026-07-26-replicated-load-balanced-serving) |
| 6 | There is one administrator | Your dependency's ops team deploys, rate-limits, or breaks you on their schedule | Bulkheads + graceful degradation ([same article](/articles/microservices/2026-07-26-timeouts-retries-bulkheads)) |
| 7 | Transport cost is zero | Serialization CPU and cross-AZ transfer fees dominate the bill | Binary encodings ([gRPC/protobuf](/articles/microservices/2026-07-27-grpc-protobuf-service-comms)), claim check |
| 8 | The network is homogeneous | Middleboxes kill idle conns; MTU mismatches; HTTP/2-unaware proxies mangle streams | Standard protocols, interop tests at the edge |

Three of these deserve more than a table row.

## Fallacy 1 is not hypothetical

Bailis and Kingsbury's [*The Network is Reliable*](https://queue.acm.org/detail.cfm?id=2655736) is a catalog of documented partitions: GitHub's 2012 switch upgrade caused an 18-minute partition in which paired file servers STONITH-killed *each other*; AWS's 2011 EBS outage started as a routing mistake that isolated nodes and triggered a "re-mirroring storm" lasting half a day; a Redis partition put Twilio's billing store into a state that overbilled 1.1% of customers in 40 minutes. The design error isn't failing to prevent partitions — you can't — it's writing code whose correctness assumes delivery. Every remote call needs a deadline, and every retry needs [backoff with jitter](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/); the corpus covers the mechanics, so here just internalize the rule: *an unanswered request is the common case you must design for, not the exception.*

## Fallacies 2 and 3 are why chatty services die at scale

Latency composes additively along a call chain; bandwidth divides across tenants. A microservice decomposition that turns one in-process call into a sequential chain of ten network hops has bought you 10× the latency floor and 10× the chances to hit fallacy 1. The classic design error is the N+1 RPC: fetch a list, then loop calling `GetDetails(id)` per item. On localhost it's invisible; across AZs it's a p99 catastrophe. Batch interfaces, data locality, and fan-out with a concurrency bound (scatter/gather) are the structural fixes — hedging and caching only trim the tail that remains.

## Fallacy 5 is the default condition in Kubernetes

Rotem-Gal-Oz warned about servers moving between writes of a config file. Today topology change is *continuous*: autoscalers, rolling deploys, spot reclaims, node drains. Anything that memoizes an endpoint — a DNS answer beyond its TTL, a keep-alive pool pinned to a dead pod, a client-side "known good replica" list — is a stale-topology bug waiting for the next deploy. Treat endpoints as leases, not facts: resolve through service discovery, honor TTLs, and let health checks eject peers instead of your retry loop discovering it per-request.

## Make one bite: a 5-minute netem experiment

The cheapest chaos experiment is [`tc netem`](https://man7.org/linux/man-pages/man8/tc-netem.8.html), which shapes egress traffic in the kernel. Point it at a test box or a network namespace — **not** your SSH interface — and watch fallacies 1 and 2 land on any service that makes sequential calls:

```bash
# Add 100ms ± 20ms latency and 1% packet loss to egress on eth0
sudo tc qdisc add dev eth0 root netem delay 100ms 20ms distribution normal loss 1%

# Baseline vs. degraded: time an endpoint that fans out internally
for i in $(seq 20); do
  curl -o /dev/null -sw '%{time_total}\n' http://testbox:8080/api/orders/42
done | sort -n | tail -3     # your new p85+; sequential callers go from ~50ms to seconds

sudo tc qdisc del dev eth0 root   # always clean up
```

An endpoint that does 30 sequential 1 ms calls now costs ~3 s — latency is not zero. The 1% loss shows fallacy 1: with TCP retransmits, some requests stall for full RTO cycles, and any missing timeout turns that into a hung worker thread. If p99 explodes while the mean barely moves, you've reproduced, on your desk, the exact shape of most real incidents.

## The meta-fallacy

Rotem-Gal-Oz's closing point is the one worth memorizing: the fallacies are dangerous precisely because the happy path hides them. Dev environments have zero latency, one administrator, and a reliable network — so code that embodies all eight assumptions passes CI. The patterns in this track are, almost without exception, machinery for surviving one specific fallacy. When you evaluate a design, walk the list: for each of the eight, ask "where does this system feel it, and what absorbs it?" A missing answer is a missing pattern.

**Try next:** run the netem experiment against a two-service demo app, then add a 250 ms deadline and one jittered retry to the inter-service call and re-run — measure how p99 and error rate change under the same 1% loss.
