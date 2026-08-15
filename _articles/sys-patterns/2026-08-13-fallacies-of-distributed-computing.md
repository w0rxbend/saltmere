---
title: "The 8 fallacies of distributed computing, thirty years on"
date: 2026-08-13
track: sys-patterns
summary: "Peter Deutsch's seven fallacies, plus Gosling's eighth, form a checklist of assumptions that compile cleanly and fail in production. Each maps to a modern incident pattern and to a mitigation this corpus already covers, and one of them can be reproduced deliberately with tc netem."
reading_time: 7
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

**Gist.** Code written against a local-call mental model embeds eight assumptions about the network that hold on a developer workstation and fail in a datacenter. The fallacies name those assumptions so that a design review can test each one explicitly: for every fallacy, where does the system feel it, and what absorbs it? The mitigations — deadlines, bounded fan-out, backpressure, service discovery, mutually authenticated transport — cost latency, code and operational surface that a local-call model does not pay.

In the early 1990s at Sun, L. Peter Deutsch wrote down seven assumptions that "essentially everyone" makes when first building a distributed application, building on an earlier list by Bill Joy and Tom Lyon; James Gosling added the eighth in 1997. Arnon Rotem-Gal-Oz's 2006 paper [*Fallacies of Distributed Computing Explained*](https://arnon.me/wp-content/uploads/Files/fallacies.pdf) remains the reference walkthrough of why each assumption hurts. The list survives because it is a taxonomy of failure modes rather than trivia: the wrapper changed — Kubernetes, service meshes, serverless — while the physics did not.

## The eight, and what each one costs

| # | Fallacy | Modern bite | Counter-pattern in this corpus |
|---|---------|-------------|-------------------------------|
| 1 | The network is reliable | Cross-AZ partition wedges leader election; requests vanish mid-flight | [Timeouts + jittered retries](/articles/microservices/2026-07-26-timeouts-retries-bulkheads), [circuit breakers](/articles/microservices/2026-07-24-circuit-breakers-resilience4j) |
| 2 | Latency is zero | An endpoint issues 30 sequential remote procedure calls (RPCs); a cross-region hop adds tens of milliseconds each | [Scatter/gather](/articles/sys-patterns/2026-07-26-scatter-gather-pattern), [request hedging](/articles/sys-patterns/2026-07-30-request-hedging-tail-latency) |
| 3 | Bandwidth is infinite | Large payloads on every event saturate a network interface; a retry storm amplifies it | [Claim check](/articles/sys-patterns/2026-07-31-claim-check-pattern), [backpressure](/articles/sys-patterns/2026-07-31-backpressure-flow-control) |
| 4 | The network is secure | Flat internal network; one server-side request forgery (SSRF) pivots to everything | [Zero-trust mTLS + JWT](/articles/microservices/2026-07-26-zero-trust-mtls-jwt) |
| 5 | Topology doesn't change | Cached DNS answers and IPs point at rescheduled pods; connections pinned to drained nodes | [Replicated, health-checked serving](/articles/sys-patterns/2026-07-26-replicated-load-balanced-serving) |
| 6 | There is one administrator | A dependency's operations team deploys, rate-limits or breaks callers on its own schedule | Bulkheads + graceful degradation ([same article](/articles/microservices/2026-07-26-timeouts-retries-bulkheads)) |
| 7 | Transport cost is zero | Serialization CPU and cross-AZ transfer fees dominate the bill | Binary encodings ([gRPC/protobuf](/articles/microservices/2026-07-27-grpc-protobuf-service-comms)), claim check |
| 8 | The network is homogeneous | Middleboxes close idle connections; maximum transmission unit (MTU) mismatches; HTTP/2-unaware proxies mangle streams | Standard protocols, interoperability tests at the edge |

Three entries deserve more than a table row.

## Fallacy 1 is documented, not hypothetical

Bailis and Kingsbury's [*The Network is Reliable*](https://queue.acm.org/detail.cfm?id=2655736) catalogues observed partitions. A 2012 network maintenance error at GitHub produced a partition in which **paired file servers each issued STONITH ("shoot the other node in the head") fencing commands against the other**. The 2011 Amazon Elastic Block Store (EBS) outage began as a network change that isolated nodes and triggered a re-mirroring storm; full recovery took days. A Redis partition left Twilio's billing store in a state that **overbilled 1.1% of customers**.

The design error is not the failure to prevent partitions, which is not achievable, but writing code whose **correctness assumes delivery**. The invariant to preserve is that no thread, lock or transaction may be held open on the expectation that a reply arrives: every remote call carries a deadline, and every retry uses [backoff with jitter](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/) so that recovering peers are not synchronised into a thundering herd. An unanswered request is the common case to design for, not the exception.

## Fallacies 2 and 3: latency composes, bandwidth divides

**Latency composes additively along a call chain; bandwidth divides across tenants.** A decomposition that turns one in-process call into a sequential chain of ten network hops multiplies the latency floor by ten and multiplies by ten the number of opportunities to encounter fallacy 1 — each hop is an independent chance of loss, and a single retry inside the chain adds its own timeout to the total.

The canonical instance is the N+1 RPC: fetch a list, then loop calling `GetDetails(id)` once per item. On loopback the per-call cost is small enough to disappear into measurement noise; across availability zones the same loop makes the tail latency proportional to the list length. The structural fixes are **batch interfaces, data locality, and fan-out with a concurrency bound**; hedging and caching only trim the tail that remains after the structure is fixed. Fallacy 3 constrains how far fan-out may go: concurrency without a bound converts a latency problem into a bandwidth and connection-pool problem.

### Implementation sketch (Scala)

The load-bearing difference between the N+1 shape and the bounded fan-out shape, with a deadline that covers the whole operation rather than each call:

```scala
import java.util.concurrent.Semaphore
import scala.concurrent.{ExecutionContext, Future, Await}
import scala.concurrent.duration.*

final class BoundedFanOut(maxInFlight: Int)(using ec: ExecutionContext):
  private val permits = Semaphore(maxInFlight)

  private def guarded[A](call: => Future[A]): Future[A] =
    permits.acquire()                       // blocks the submitter, propagating backpressure
    call.andThen { case _ => permits.release() }

  /** One deadline for the whole batch: a slow tail cannot extend the total. */
  def details(ids: Seq[Long], budget: FiniteDuration)(
      fetch: Long => Future[Detail]): Seq[Detail] =
    val deadline = Deadline.now + budget
    val all = Future.sequence(ids.map(id => guarded(fetch(id))))
    Await.result(all, deadline.timeLeft)    // throws TimeoutException at the budget

// Sequential shape, for contrast: total latency is ids.size * RTT.
def detailsSequential(ids: Seq[Long])(fetch: Long => Detail): Seq[Detail] =
  ids.map(fetch)
```

The semaphore is the fallacy-3 bound and the deadline is the fallacy-1 bound; **omitting either leaves the other ineffective**, because an unbounded fan-out saturates the link before the deadline fires, and an unbounded wait holds the caller's thread after the link recovers.

## Fallacy 5 is the default condition under orchestration

Rotem-Gal-Oz described servers moving between edits of a configuration file. Under an orchestrator, topology change is continuous: autoscaling, rolling deployments, spot reclaims, node drains. Anything that memoises an endpoint — a DNS answer retained beyond its time to live (TTL), a keep-alive pool pinned to a terminated pod, a client-side list of known-good replicas — is a stale-topology defect that surfaces at the next deployment. **Endpoints are leases, not facts**: resolve through service discovery, honour TTLs, and let health checks eject peers rather than having the retry loop rediscover the failure once per request.

## Reproducing a bite: a netem experiment

[`tc netem`](https://man7.org/linux/man-pages/man8/tc-netem.8.html) shapes egress traffic in the kernel and is the cheapest way to make fallacies 1 and 2 observable. It must be applied to a test host or a network namespace, **never to the interface carrying the operator's own SSH session**, because the shaping applies to that traffic as well.

```bash
# Add 100ms ± 20ms latency and 1% packet loss to egress on eth0
sudo tc qdisc add dev eth0 root netem delay 100ms 20ms distribution normal loss 1%

# Baseline vs. degraded: time an endpoint that fans out internally
for i in $(seq 20); do
  curl -o /dev/null -sw '%{time_total}\n' http://testbox:8080/api/orders/42
done | sort -n | tail -3     # the upper tail; sequential callers degrade to seconds

sudo tc qdisc del dev eth0 root   # always clean up
```

An endpoint performing 30 sequential 1 ms calls costs roughly 3 s under 100 ms of added delay. The 1% loss exposes fallacy 1: TCP retransmission means some requests stall for full retransmission-timeout cycles, and a call site without a timeout converts that stall into a hung worker thread. **A p99 that explodes while the mean barely moves is the characteristic shape of the failure**, and it is reproducible on a single host.

## The meta-fallacy

Rotem-Gal-Oz's closing point is that the fallacies are dangerous precisely because the happy path hides them. A development environment has near-zero latency, one administrator and a reliable network, so code embodying all eight assumptions passes continuous integration unchanged. Most patterns in this track are machinery for surviving one specific fallacy; walking the list of eight against a design and finding no answer for an entry identifies a missing pattern rather than an absent risk.

## Pitfalls

- **Per-call timeouts instead of a per-operation budget.** A chain of ten calls each with a 1 s timeout can take 10 s; the caller's own deadline expires while downstream work is still in flight, and the work is never cancelled.
- **Retries without jitter.** Peers that failed together back off together and retry in phase, so the recovering dependency receives a synchronised burst rather than a ramp.
- **Retrying at every layer.** A retry in the client, the proxy and the service multiplies attempts, so a single user request can become a multiplicative number of backend calls during the exact interval when the backend is already saturated.
- **Unbounded fan-out as the fix for sequential latency.** Replacing an N+1 loop with N concurrent calls trades fallacy 2 for fallacy 3: the connection pool and the link saturate, and queueing delay reappears as tail latency.
- **Caching a resolved address beyond its TTL.** The client keeps sending to a terminated pod's address and observes connection refusals or, worse, a reassigned address belonging to a different workload.
- **Treating an internal network as a trust boundary.** With no per-hop authentication, one SSRF or one compromised pod reaches every service that assumed the caller was already vetted.
- **Applying `tc netem` to the administration interface.** The shaping applies to the SSH session used to remove it, so a high loss or delay setting can make the host unreachable before the cleanup command runs.
- **Testing only the mean.** Injected loss moves the upper percentiles first; a load test reporting average latency will show a degradation of a few milliseconds while p99 moves by seconds.
