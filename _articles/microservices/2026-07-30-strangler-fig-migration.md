---
title: "Strangler Fig: decomposing a monolith one route at a time"
date: 2026-07-30
track: microservices
summary: "Big-bang replacements of legacy systems fail because the undocumented behaviour is discovered only when the rewrite gets it wrong in production. The Strangler Fig pattern interposes a proxy in front of the monolith and moves one capability at a time, routing individual endpoints to new services while the remainder keeps serving. The mechanism covers the interception layer, the routing table, the canary, and the data-ownership problem that dominates the effort."
reading_time: 7
tags: [strangler-fig, monolith, migration, reverse-proxy, feature-flags, cdc]
sources:
  - title: "StranglerFigApplication — Martin Fowler"
    url: "https://martinfowler.com/bliki/StranglerFigApplication.html"
  - title: "Monolith to Microservices — Sam Newman"
    url: "https://samnewman.io/books/monolith-to-microservices/"
  - title: "Strangler Fig pattern — Azure Architecture Center"
    url: "https://learn.microsoft.com/en-us/azure/architecture/patterns/strangler-fig"
  - title: "Strangler fig pattern — AWS Prescriptive Guidance"
    url: "https://docs.aws.amazon.com/prescriptive-guidance/latest/modernization-decomposing-monoliths/strangler-fig.html"
---

**Gist.** Replacing a large legacy system wholesale fails because the expensive part of that system is behaviour nobody documented, and the divergence surfaces only in production. The Strangler Fig pattern interposes an interception layer — a reverse proxy — between clients and the monolith, so that replacement proceeds one route at a time, each slice individually reversible. The cost is a prolonged period of coexistence: two implementations of the same capability run simultaneously, the data must be kept consistent across both, and every request pays the proxy's latency.

Martin Fowler named the pattern after a plant he encountered on a trip to Australia: strangler figs germinate high on a host tree, send roots down around its trunk, and grow into a self-supporting shape while the original tree rots away inside. The migration has the same shape — the new system grows around the edges of the monolith until the monolith is hollow and can be removed, with no flag day and no code freeze.

## The interception layer

Everything rests on one component between clients and the monolith. Fowler and the Azure and AWS write-ups call it a *facade* or *interception layer*; in practice it is a reverse proxy. **Clients continue to address the same host, and the proxy decides per request whether the call reaches the monolith or a new service.**

The ordering of the two changes is load-bearing. **On day one the proxy forwards 100% of traffic to the monolith and does nothing else**, so the interceptor is installed and validated while its behaviour is still the identity function. If the proxy is introduced at the same moment as the first migrated capability, a production incident has two candidate causes and neither can be excluded by rollback of the other.

Once the proxy is in place, migrating a capability is a routing edit. NGINX peeling the shipping-quote endpoint off to a new service:

```nginx
upstream monolith   { server monolith.internal:8080; }
upstream shipping_v2 { server shipping-svc.internal:9090; }

server {
  listen 80;

  # Peeled off: this one path now goes to the new service
  location = /api/shipping/quote {
    proxy_pass http://shipping_v2;
  }

  # Everything else still hits the monolith
  location / {
    proxy_pass http://monolith;
  }
}
```

The `location` blocks constitute the migration ledger: specific routes at the top point at new services, and the catch-all `/` at the bottom is the shrinking monolith. **Match order is a correctness condition — an exact or prefix match placed below the fallthrough never receives traffic, and the new service appears healthy while serving nothing.** Envoy, HAProxy, an API gateway or a service mesh perform the same function; NGINX is the smallest configuration that exhibits the shape.

## Choosing the seam

A capability is a viable first slice when it has a hypertext transfer protocol (HTTP) surface nameable by a route, reads and writes a bounded set of data, and is valuable enough that shipping it demonstrates the migration works. Newman's advice in *Monolith to Microservices* is to begin with something low-risk and high-learning: the deployment pipeline and the on-call procedures are being migrated alongside the code.

The failure mode is an endpoint that appears independent and is not. `/api/shipping/quote` is a sound candidate if it consumes only address and weight. It is a trap if, several calls deep, the monolith's quote logic also decrements inventory and writes to the orders table — **the seam then cuts through a write path, and two systems contend for the same rows.**

## Canary and reversibility

A route is not flipped from monolith to new service in a single commit. The interception layer is where the **canary** lives: a small fraction of `/api/shipping/quote` is sent to `shipping_v2`, its results and latency compared against the monolith, and the fraction increased. Two mechanisms are in common use:

- **Weighted routing** at the proxy. Envoy and most gateways express this as weighted clusters, and the weights are moved gradually toward the new service.
- **A feature toggle** consulted per request, so the new path can be shifted or disabled without a redeployment.

**Reversibility holds only while the monolith path still exists and is still correct.** During that window rollback is a weight change or a toggle flip; the published descriptions of the pattern all treat this coexistence phase as the point of it. Deleting the old code path ends the window: from that point a defect in the new service requires a forward fix under incident conditions.

**Shadow traffic** narrows the risk further: requests are mirrored to `shipping_v2`, its responses discarded, and the two outputs compared offline. This exercises the undocumented-behaviour cases against real inputs without any user observing the new service's output.

### Implementation sketch (Scala)

The interceptor's decision function, isolated from transport concerns. The routing table is an ordered sequence, so precedence is explicit rather than emergent, and the canary weight is derived from a stable request key so that one client does not oscillate between implementations.

```scala
enum Target { case Monolith, Candidate }

final case class Route(
    matches: String => Boolean,
    candidateWeight: Int, // 0..100; 0 keeps the route on the monolith
    shadow: Boolean       // mirror to candidate, discard its response
)

final class Interceptor(table: Seq[(String, Route)]):

  /** First match wins: entries earlier in `table` take precedence. */
  private def lookup(path: String): Option[Route] =
    table.collectFirst { case (_, r) if r.matches(path) => r }

  /** Stable across retries of the same request, so a client does not flip
    * between implementations mid-session. */
  private def bucket(key: String): Int =
    math.floorMod(scala.util.hashing.MurmurHash3.stringHash(key), 100)

  def decide(path: String, sessionKey: String): (Target, Boolean) =
    lookup(path) match
      case None => (Target.Monolith, false)
      case Some(r) =>
        val target =
          if bucket(sessionKey) < r.candidateWeight then Target.Candidate
          else Target.Monolith
        (target, r.shadow && target == Target.Monolith)
```

`decide` returns the serving target and whether to mirror. Mirroring is suppressed when the candidate already serves the request, which would otherwise double the load it receives.

## Data ownership

Routing HTTP requests is the tractable half. The difficulty concentrates where the new service and the monolith require the same data.

| Stage | What the new service does with data | Risk |
|---|---|---|
| Shared DB | Reads/writes the monolith's tables directly | Fast to ship, but decoupling has not occurred — the schema remains a shared contract |
| CDC sync | Owns a new store; change data capture (CDC) streams the monolith's writes into it | Eventual consistency; lag and replay must be handled |
| System of record | New store is authoritative; monolith reads *from* the service | Cutover of writes is the genuinely hazardous step |

The sequence the write-ups converge on is: the new service first reads and writes the shared monolith database; its tables are then extracted into a domain-owned store; CDC keeps the two in sync; the new store is finally promoted to system of record and the old tables removed. **An anti-corruption layer sits between the new service's model and the monolith's schema**, so the legacy data model is translated at the boundary rather than propagating into the new design.

## When the pattern does not apply

- **The calls cannot be intercepted.** The pattern presupposes that a facade can be placed in front and can route. Where clients reach the backend over channels that admit no such facade, there is nothing to strangle.
- **The system is small.** If replacement in a single step is achievable, the transitional proxy, dual data paths and CDC plumbing are pure overhead. Fowler presents the pattern as the alternative to rewriting a system in a single step.
- **Decommissioning is urgent.** Coexistence can run for months. A compliance deadline or licence expiry that forces rapid removal works against the pattern.
- **Source access to the monolith is unavailable.** Disabling the migrated feature inside the old code and redirecting its internal calls generally requires modifying it; a black-box binary bounds how completely it can be strangled.

The interceptor is on the path of every request. It is a single point of failure and a latency tax on all traffic, migrated or not, and its value derives from being the one component whose behaviour is stable while everything behind it changes.

## Pitfalls

- A prefix or exact `location` placed below the catch-all `/` never matches; the new service reports zero errors because it receives zero traffic, and the migration appears complete while the monolith still serves the route.
- Introducing the proxy and the first migrated route in the same change leaves an incident with two candidate causes, and rolling back one does not exonerate the other.
- Choosing a seam whose handler writes to shared tables produces two writers on the same rows; the symptom is lost updates or constraint violations under concurrency, not a routing error.
- Canary assignment computed per request rather than from a stable key sends successive calls of one session to different implementations, so a client observes state written by one and absent from the other.
- Mirroring shadow traffic to a candidate that also serves live traffic doubles its load, and a capacity failure is then misread as the new implementation being too slow.
- Deleting the monolith's code path before the canary has run at full weight removes the rollback mechanism; the next defect must be fixed forward during an incident.
- Treating CDC replication as synchronous means the new service reads a store lagging the monolith; the symptom is a read-after-write anomaly for users whose write went to the monolith and whose read went to the service.
- Allowing the monolith's schema into the new service's domain model — omitting the anti-corruption layer — makes the legacy shape permanent, since it must then be preserved when the old tables are dropped.
